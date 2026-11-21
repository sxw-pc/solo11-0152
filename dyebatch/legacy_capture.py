"""Capture offline register exports from a legacy shade instrument.

Two register formats exist in the field and the protocol field of this
export's record header is illegible, so the raw words alone do not
determine the measurements:

- ``absolute-sample``: every word is the measured value in tenths
  (word 14 -> delta_e 1.4).
- ``signed-differences``: after an absolute anchor read out from the
  device, each signed word shifts the previous raw value (anchor 11,
  word 14 -> raw 25 -> delta_e 2.5).

Decoding rules enforced here:

- A page break is layout only; it never resets the accumulation.  Only
  a real device restart with a read-out absolute anchor opens a new
  accumulation round.
- If a page is lost, a later absolute anchor restores the words after
  it; earlier differences whose predecessor is missing stay undecoded
  and may not pose as known.
- Every word of one run round is decoded with one format.  Formats are
  never chosen per value and never reverse-guessed from the customer
  threshold.
- A record whose decoded value is identical under every admissible
  format enters the ledger at once; a record whose business verdict
  would not change but whose true value still differs stays pending -
  no row is fabricated for it.

Two evidence routes can certify the format of a round, each usable
once per round:

- ``register-archive``: the factory archive original with the
  pre-preserved device configuration, the original file layout range
  and the round-bound format header.  A device model name alone, or a
  header belonging to a copied adjacent page, has no real version
  connection and determines nothing (but still spends the route).
- ``register-diagnose``: a read-only bench probe executed in the same
  live export round with a known seed (previous value) and a known
  test truth.  A probe whose encoding is identical under every
  admissible format (seed 0 / test 4 reads 4 either way) is redundant
  and does not spend the opportunity; a decisive probe (seed 10 /
  test 4 reads 4 absolute vs -6 differential) settles the round.  A
  diagnostic binds only the round it runs in; it cannot prove the
  format from before a restart.

Delivery contract with dyebatch.shade: only certified (or
unique-across-formats) decodes are handed to ``shade.import_data`` as
``kind='measurement'`` samples.  Each delivered sample carries a
``provenance`` object citing the source file excerpt, the record id,
the protocol evidence and the true base value in force at the time;
shade.py persists the measurement and keeps the whole payload in the
evidence journal.  Deliveries are idempotent, so replaying the file
journal reproduces the same ledger.
"""

import hashlib
import json

from . import shade
from .source import record

SCHEMES = ("absolute-sample", "signed-differences")


def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _excerpt_id(excerpt):
    sha = hashlib.sha256(_canonical(excerpt)).hexdigest()[:12]
    return "{}-excerpt-{}".format(excerpt["legacy_session"], sha)


def _record_excerpt(db, excerpt):
    excerpt_id = _excerpt_id(excerpt)
    item = dict(excerpt)
    item["id"] = excerpt_id
    record(db, "register_stream", item)
    return excerpt_id


def _ensure_stream(db, excerpt, excerpt_id):
    """Merge a register file excerpt into the session stream (idempotent)."""
    session = excerpt["legacy_session"]
    if db.execute(
        "SELECT 1 FROM register_round WHERE session=? AND excerpt_id=?",
        (session, excerpt_id),
    ).fetchone():
        return session
    last = db.execute(
        "SELECT COALESCE(MAX(round_no),0) AS m FROM register_round WHERE session=?",
        (session,),
    ).fetchone()["m"]
    anchors = excerpt.get("anchors") or []
    anchor = anchors[-1]["value"] if anchors else None
    if last == 0:
        round_no = 1
    elif excerpt.get("restart") and anchor is not None:
        # only a real device restart with a read-out absolute anchor
        # may open a new accumulation round
        round_no = last + 1
    else:
        raise ValueError(
            "register stream continuation requires a real restart anchor: " + session
        )
    db.execute(
        "INSERT INTO register_round(session,round_no,anchor,excerpt_id) VALUES(?,?,?,?)",
        (session, round_no, anchor, excerpt_id),
    )
    page = 1
    break_after = excerpt.get("pagination_break_after")
    broken = False
    for seq, word in enumerate(excerpt["raw_sequence"]):
        db.execute(
            "INSERT INTO register_word(session,round_no,seq,word,page) VALUES(?,?,?,?,?)",
            (session, round_no, seq, int(word), page),
        )
        if not broken and break_after is not None and word == break_after:
            page += 1  # layout only; the accumulation is not affected
            broken = True
    return session


def _map_records(db, data, session, excerpt_id):
    """Bind each business record to its raw word inside its own file excerpt."""
    rnd = db.execute(
        "SELECT * FROM register_round WHERE session=? AND excerpt_id=?",
        (session, excerpt_id),
    ).fetchone()
    words = db.execute(
        "SELECT * FROM register_word WHERE session=? AND round_no=? ORDER BY seq",
        (session, rnd["round_no"]),
    ).fetchall()
    taken = set()
    for row in data.get("records", []):
        if not record(db, "legacy_register", row):
            continue  # already evidenced, hence already mapped
        if row.get("block") not in (None, session):
            raise ValueError("record block does not match register session: " + row["id"])
        for w in words:
            if w["seq"] in taken or w["record_id"] is not None:
                continue
            if w["word"] == int(row["register"]):
                taken.add(w["seq"])
                db.execute(
                    "UPDATE register_word SET record_id=? WHERE session=? AND round_no=? AND seq=?",
                    (row["id"], session, rnd["round_no"], w["seq"]),
                )
                break
        else:
            raise ValueError("record register not found in the raw sequence: " + row["id"])


def _decode(words, anchor, scheme):
    """Decode one round's words; returns (decoded, base) per word or None."""
    if scheme == "absolute-sample":
        return [w for w in words], [None] * len(words)
    decoded, bases, prev = [], [], anchor
    for w in words:
        if prev is None:
            # a lost predecessor must not pose as known
            decoded.append(None)
            bases.append(None)
        else:
            bases.append(prev)
            prev = prev + w
            decoded.append(prev)
    return decoded, bases


def _excerpt_of(db, rnd):
    raw = db.execute(
        "SELECT raw FROM evidence WHERE id=?", (rnd["excerpt_id"],)
    ).fetchone()["raw"]
    return json.loads(raw)


def _schemes_of(db, rnd):
    return tuple(_excerpt_of(db, rnd).get("schemes") or SCHEMES)


def _decode_candidates(db, rnd, words):
    """Decoded value per scheme for every word of the round."""
    raw_words = [w["word"] for w in words]
    return {s: _decode(raw_words, rnd["anchor"], s) for s in _schemes_of(db, rnd)}


def _normalize(db, session):
    """Deliver every decodable pending record of the session to shade."""
    deliver = []
    rounds = db.execute(
        "SELECT * FROM register_round WHERE session=? ORDER BY round_no", (session,)
    ).fetchall()
    for rnd in rounds:
        words = db.execute(
            "SELECT * FROM register_word WHERE session=? AND round_no=? ORDER BY seq",
            (session, rnd["round_no"]),
        ).fetchall()
        if not any(w["record_id"] and not w["delivered"] for w in words):
            continue
        if rnd["format"]:
            scheme = rnd["format"]
            certificate = rnd["certificate"]
            decoded, bases = _decode([w["word"] for w in words], rnd["anchor"], scheme)
        else:
            # without a certificate only a value identical under every
            # admissible format may enter the ledger
            per_scheme = _decode_candidates(db, rnd, words)
            decoded, bases = [], []
            for i in range(len(words)):
                values = {d[0][i] for d in per_scheme.values()}
                base_values = {d[1][i] for d in per_scheme.values()}
                if len(values) == 1:
                    decoded.append(values.pop())
                    bases.append(base_values.pop() if len(base_values) == 1 else None)
                else:
                    decoded.append(None)
                    bases.append(None)
            scheme = "unique-across-schemes"
            certificate = rnd["excerpt_id"]
        for w, value, base in zip(words, decoded, bases):
            if not w["record_id"] or w["delivered"] or value is None:
                continue
            db.execute(
                "UPDATE register_word SET decoded=?, base=?, delivered=1 WHERE session=? AND round_no=? AND seq=?",
                (value, base, session, rnd["round_no"], w["seq"]),
            )
            deliver.append((rnd, w, value, base, scheme, certificate))
    if not deliver:
        return []
    samples = []
    for rnd, w, value, base, scheme, certificate in deliver:
        row = json.loads(
            db.execute("SELECT raw FROM evidence WHERE id=?", (w["record_id"],)).fetchone()["raw"]
        )
        samples.append(
            {
                "id": row["id"] + "-shade",
                "order_id": row["order_id"],
                "instrument_id": row["instrument_id"],
                "at": row["at"],
                "replicate": row["replicate"],
                "delta_e": value / 10,
                "kind": "measurement",
                "provenance": {
                    "source_file": rnd["excerpt_id"],
                    "source_record": row["id"],
                    "protocol_evidence": certificate,
                    "scheme": scheme,
                    "base_value": base,
                    "raw_word": w["word"],
                    "decoded_register": value,
                },
            }
        )
    return shade.import_data(db, {"samples": samples})


def _pending(db, session):
    """Undecoded records of the session with their per-format candidates."""
    pending = []
    rounds = db.execute(
        "SELECT * FROM register_round WHERE session=? ORDER BY round_no", (session,)
    ).fetchall()
    for rnd in rounds:
        words = db.execute(
            "SELECT * FROM register_word WHERE session=? AND round_no=? ORDER BY seq",
            (session, rnd["round_no"]),
        ).fetchall()
        undecided = [w for w in words if w["record_id"] and not w["delivered"]]
        if not undecided:
            continue
        per_scheme = _decode_candidates(db, rnd, words)
        records = []
        for w in undecided:
            candidates = {}
            for s, (decoded, _bases) in per_scheme.items():
                value = decoded[w["seq"]]
                candidates[s] = value / 10 if value is not None else None
            records.append(
                {
                    "record": w["record_id"],
                    "word": w["word"],
                    "page": w["page"],
                    "candidates": candidates,
                }
            )
        pending.append(
            {
                "session": session,
                "round": rnd["round_no"],
                "format": rnd["format"],
                "records": records,
            }
        )
    return pending


def _probe_encodings(probe):
    """Raw word the device would emit for the probe under each format."""
    return {
        "absolute-sample": probe["test"],
        "signed-differences": probe["test"] - probe["seed"],
    }


def _plan(db, session):
    """Executable capture actions for the rounds that still lack a format."""
    rounds = db.execute(
        "SELECT * FROM register_round WHERE session=? ORDER BY round_no", (session,)
    ).fetchall()
    latest = rounds[-1]["round_no"] if rounds else None
    actions = []
    for rnd in rounds:
        if rnd["format"] is not None:
            continue
        words = db.execute(
            "SELECT * FROM register_word WHERE session=? AND round_no=? ORDER BY seq",
            (session, rnd["round_no"]),
        ).fetchall()
        undecided = [w for w in words if w["record_id"] and not w["delivered"]]
        if not undecided:
            continue
        excerpt = _excerpt_of(db, rnd)
        per_scheme = _decode_candidates(db, rnd, words)
        decodes = {}
        for s, (decoded, _bases) in per_scheme.items():
            decodes[s] = {
                w["record_id"]: (decoded[w["seq"]] / 10 if decoded[w["seq"]] is not None else None)
                for w in undecided
            }
        if not rnd["archive_used"]:
            actions.append(
                {
                    "action": "factory_archive",
                    "session": session,
                    "round": rnd["round_no"],
                    "request": "pre-preserved device_config, original layout_range and round-bound format_header",
                    "outcomes": [
                        {"return": {"format_header": {"scheme": s}}, "decodes": decodes[s]}
                        for s in decodes
                    ]
                    + [
                        {
                            "return": {"device_model_only": True},
                            "decodes": {},
                            "note": "a model name or an adjacent-page header has no real version connection; records stay undecoded",
                        }
                    ],
                }
            )
        if not rnd["diagnostic_used"]:
            if rnd["round_no"] != latest:
                actions.append(
                    {
                        "action": "online_diagnostic",
                        "session": session,
                        "round": rnd["round_no"],
                        "decisive": False,
                        "note": "a bench diagnostic binds only the live round; it cannot prove the format from before a restart",
                    }
                )
            else:
                decisive, redundant = [], []
                for probe in excerpt.get("safe_bench_probes") or []:
                    enc = _probe_encodings(probe)
                    (redundant if len(set(enc.values())) == 1 else decisive).append((probe, enc))
                if decisive:
                    probe, enc = decisive[0]
                    actions.append(
                        {
                            "action": "online_diagnostic",
                            "session": session,
                            "round": rnd["round_no"],
                            "probe": probe,
                            "outcomes": [
                                {"observed": code, "certifies": s, "decodes": decodes[s]}
                                for s, code in enc.items()
                            ],
                            "redundant_probes": [
                                {
                                    "probe": p,
                                    "reason": "every admissible format encodes it as {}; running it proves nothing".format(
                                        next(iter(e.values()))
                                    ),
                                }
                                for p, e in redundant
                            ],
                        }
                    )
                else:
                    actions.append(
                        {
                            "action": "online_diagnostic",
                            "session": session,
                            "round": rnd["round_no"],
                            "decisive": False,
                            "note": "available probes cannot distinguish the formats; do not run a blind probe expecting new evidence",
                            "redundant_probes": [
                                {
                                    "probe": p,
                                    "reason": "every admissible format encodes it as {}".format(
                                        next(iter(_probe_encodings(p).values()))
                                    ),
                                }
                                for p, _e in redundant
                            ],
                        }
                    )
    if not actions:
        return None
    return {
        "session": session,
        "budget": "only one new evidence source is obtainable; choose one route",
        "actions": actions,
    }


def import_data(db, data):
    """Import a register export; undecoded words stay pending with a plan."""
    excerpt = data.get("register_file_excerpt")
    if not excerpt:
        raise ValueError(
            "register_file_excerpt required: raw words are not decodable without the stream evidence"
        )
    with db:
        excerpt_id = _record_excerpt(db, excerpt)
        session = _ensure_stream(db, excerpt, excerpt_id)
        _map_records(db, data, session, excerpt_id)
        imported = _normalize(db, session)
        pending = _pending(db, session)
        return {
            "session": session,
            "excerpt": excerpt_id,
            "imported": imported,
            "pending": pending,
            "plan": _plan(db, session) if pending else None,
        }


def _archive_sufficient(db, data, rnd, schemes):
    header = data.get("format_header") or {}
    if not data.get("device_config") or not data.get("layout_range"):
        return (
            False,
            "archive has no real version connection: pre-preserved device_config and original layout_range are required; a device model name alone proves nothing",
        )
    if header.get("scheme") not in schemes:
        return False, "archive has no round-bound format header"
    if header.get("page", "original") != "original":
        return False, "archive header belongs to a copied adjacent page, not to this file"
    if header.get("round") is None:
        return False, "archive header is not bound to this run round"
    if header.get("round") != rnd["round_no"]:
        return False, "archive header is bound to a different run round"
    pages = {
        r[0]
        for r in db.execute(
            "SELECT DISTINCT page FROM register_word WHERE session=? AND round_no=?",
            (rnd["session"], rnd["round_no"]),
        )
    }
    claimed = set((data.get("layout_range") or {}).get("pages") or [])
    if not pages <= claimed:
        return False, "archive layout range does not cover the original file pages"
    return True, None


def apply_archive(db, data):
    """Certify a round's format from a factory archive original."""
    with db:
        if "id" not in data or "session" not in data:
            raise ValueError("archive original requires id and session")
        record(db, "register_archive", data)
        session = data["session"]
        header = data.get("format_header") or {}
        round_no = header.get("round") or data.get("round")
        if round_no is None:
            round_no = db.execute(
                "SELECT MAX(round_no) AS m FROM register_round WHERE session=?", (session,)
            ).fetchone()["m"]
        if round_no is None:
            raise ValueError("archive names an unknown register session: " + session)
        rnd = db.execute(
            "SELECT * FROM register_round WHERE session=? AND round_no=?",
            (session, round_no),
        ).fetchone()
        if rnd is None:
            raise ValueError(
                "archive names a round that does not exist: {} round {}".format(session, round_no)
            )
        schemes = _schemes_of(db, rnd)
        scheme = header.get("scheme")
        if rnd["format"] is not None:
            if scheme in (None, rnd["format"]):
                return {
                    "certified": True,
                    "redundant": True,
                    "session": session,
                    "round": round_no,
                    "format": rnd["format"],
                    "imported": [],
                    "pending": _pending(db, session),
                }
            raise ValueError("conflicting protocol evidence for an already certified round")
        if rnd["archive_used"]:
            raise ValueError("factory archive route already used for this round")
        db.execute(
            "UPDATE register_round SET archive_used=1 WHERE session=? AND round_no=?",
            (session, round_no),
        )
        sufficient, reason = _archive_sufficient(db, data, rnd, schemes)
        if not sufficient:
            return {
                "certified": False,
                "session": session,
                "round": round_no,
                "reason": reason,
                "imported": [],
                "pending": _pending(db, session),
                "plan": _plan(db, session),
            }
        db.execute(
            "UPDATE register_round SET format=?, certificate=? WHERE session=? AND round_no=?",
            (scheme, data["id"], session, round_no),
        )
        return {
            "certified": True,
            "session": session,
            "round": round_no,
            "format": scheme,
            "imported": _normalize(db, session),
            "pending": _pending(db, session),
        }


def apply_diagnostic(db, data):
    """Certify the live round's format from a read-only bench diagnostic."""
    with db:
        if "id" not in data or "session" not in data:
            raise ValueError("diagnostic original requires id and session")
        record(db, "register_diagnostic", data)
        session = data["session"]
        rnd = db.execute(
            "SELECT * FROM register_round WHERE session=? ORDER BY round_no DESC LIMIT 1",
            (session,),
        ).fetchone()
        if rnd is None:
            raise ValueError("diagnostic names an unknown register session: " + session)
        excerpt = _excerpt_of(db, rnd)
        schemes = _schemes_of(db, rnd)
        probe = data.get("probe") or {}
        sanctioned = excerpt.get("safe_bench_probes") or []
        if not any(
            p.get("seed") == probe.get("seed") and p.get("test") == probe.get("test")
            for p in sanctioned
        ):
            raise ValueError("probe is not one of the sanctioned read-only bench probes for this round")
        if "observed" not in data:
            raise ValueError("diagnostic observation required")
        enc = _probe_encodings(probe)
        matches = [s for s in schemes if enc.get(s) == data["observed"]]
        if len(matches) == len(schemes):
            return {
                "certified": False,
                "redundant": True,
                "session": session,
                "round": rnd["round_no"],
                "format": rnd["format"],
                "reason": "probe reading is identical under every admissible format; the diagnostic opportunity is not consumed",
                "imported": [],
                "pending": _pending(db, session),
                "plan": _plan(db, session),
            }
        if not matches:
            raise ValueError("diagnostic observation matches no admissible format")
        scheme = matches[0]
        if rnd["format"] is not None:
            if rnd["format"] == scheme:
                return {
                    "certified": True,
                    "redundant": True,
                    "session": session,
                    "round": rnd["round_no"],
                    "format": rnd["format"],
                    "imported": [],
                    "pending": _pending(db, session),
                }
            raise ValueError("conflicting protocol evidence for an already certified round")
        if rnd["diagnostic_used"]:
            raise ValueError("online diagnostic route already used for this round")
        db.execute(
            "UPDATE register_round SET diagnostic_used=1, format=?, certificate=? WHERE session=? AND round_no=?",
            (scheme, data["id"], session, rnd["round_no"]),
        )
        return {
            "certified": True,
            "session": session,
            "round": rnd["round_no"],
            "format": scheme,
            "imported": _normalize(db, session),
            "pending": _pending(db, session),
        }
