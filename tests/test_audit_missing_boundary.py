import copy
import hashlib
import json
from pathlib import Path

import pytest

from dyebatch import recipe, report, shade, vat
from dyebatch.source import digest_of, file_record, record
from dyebatch.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def seed_file(db, command, handler, data):
    """Business import plus the surviving whole-file journal entry."""
    handler(db, data)
    with db:
        file_record(db, command, json.dumps(data).encode("utf-8"))


def seed_anchor_files(db):
    base = read("recipe_ok.json")
    seed_file(db, "recipe-import", recipe.import_data, base)
    seed_file(db, "vat-plan", vat.import_data, read("vat_ok.json"))
    seed_file(db, "shade-import", shade.import_data, read("shade_ok.json"))
    return base


def build_gap_db(*, split, items, followup_id, followup_version=2):
    """Replay the real counter-example: business imports for the gap samples are
    complete, but their whole-file records are missing. Only anchor files keep a
    journal entry. `split` chooses one of the two indistinguishable histories."""
    db = connect(":memory:")
    base = seed_anchor_files(db)
    if split:
        for sample in items:
            shade.import_data(db, {"samples": [sample]})
    else:
        shade.import_data(db, {"samples": items})
    revision = dict(base["revisions"][0], id=followup_id,
                    version=followup_version, approved_by="factory")
    seed_file(db, "recipe-import", recipe.import_data, {"revisions": [revision]})
    return db


@pytest.fixture
def counterexample():
    raw = read("audit_missing_shade_boundary.json")
    items = [raw["first"], raw["second"]]
    grouped = build_gap_db(split=False, items=items, followup_id=raw["followup_id"])
    separate = build_gap_db(split=True, items=items, followup_id=raw["followup_id"])
    yield grouped, separate, items
    grouped.close()
    separate.close()


def sha_map(db):
    return {row["id"]: row["sha"] for row in db.execute("SELECT id,sha FROM evidence")}


def handoff(raw_bytes, source, **position):
    return {"type": "handoff_file", "command": "shade-import", "raw": raw_bytes,
            "attested_sha": hashlib.sha256(raw_bytes).hexdigest(),
            "source": source, "position": position}


def manifest_relation(db, mode, id_a, id_b, source, *, before, after):
    shas = sha_map(db)
    return {"type": "boundary_manifest", "source": source, "manifest": {
        "relations": [{"mode": mode, "item_sha": shas[id_a],
                       "other_item_sha": shas[id_b]}],
        "anchors": [{"role": "before", "item_sha": shas[before]},
                    {"role": "after", "item_sha": shas[after]}]}}


# ---------------------------------------------------------------------------
# 1. The real counter-example: identical surviving evidence, two real histories
# ---------------------------------------------------------------------------

def test_machine_independent_audit_replays_both_legal_histories(counterexample):
    grouped, separate, items = counterexample
    assert report.audit(grouped) == report.audit(separate)
    audit = report.audit(grouped)
    assert audit["ok"] is False and audit["files"] == 4
    assert audit["gap"]["item_insertion_order"] == [i["id"] for i in items]
    assert audit["gap"]["anchors"]["before"]["id"] == "SAMPLE-2"
    assert audit["gap"]["anchors"]["after"]["id"] == "RECIPE-FACTORY-V2"
    assert audit["business_proof"]["status"] == "indeterminate"
    assert audit["business_proof"]["booking_allowed"] is None
    states = {h["recipe_state"]: h for h in audit["possible_histories"]}
    assert set(states) == {"approved", "rework_approval_pending"}
    assert states["approved"]["lock"] is None
    assert states["rework_approval_pending"]["lock"]["state"] == "blocked"
    assert audit["file_proof"]["status"] == "missing"
    need_actions = [n["action"] for n in audit["evidence_needs"]]
    assert "retrieve_boundary_manifest" in need_actions


def test_identical_evidence_and_journal_on_both_machines(counterexample):
    grouped, separate, _items = counterexample
    cols = "SELECT id,kind,sha,raw FROM evidence ORDER BY rowid"
    assert [tuple(r) for r in grouped.execute(cols)] == \
           [tuple(r) for r in separate.execute(cols)]
    jcols = "SELECT command,sha,raw FROM file_journal ORDER BY id"
    assert [tuple(r) for r in grouped.execute(jcols)] == \
           [tuple(r) for r in separate.execute(jcols)]
    assert grouped.execute("SELECT count(*) FROM file_journal").fetchone()[0] == 4


# ---------------------------------------------------------------------------
# 2. Type-1 evidence: original whole files with shift-attested digest
# ---------------------------------------------------------------------------

def test_type1_grouped_whole_file_proves_booking_and_completes_file_proof(counterexample):
    grouped, separate, items = counterexample
    raw = json.dumps({"samples": items}, separators=(",", ":")).encode()
    witnesses = [handoff(raw, "handoff-archive-A",
                         after_anchor="SAMPLE-2", before_anchor="RECIPE-FACTORY-V2")]
    for db in (grouped, separate):
        audit = report.audit(db, witnesses)
        assert audit["business_proof"]["status"] == "proven"
        assert audit["business_proof"]["value"] == "approved"
        assert audit["business_proof"]["booking_allowed"] is True
        assert len(audit["possible_histories"]) == 1
        assert audit["possible_histories"][0]["groups"] == [[i["id"] for i in items]]
        assert audit["file_proof"]["status"] == "complete"
        assert audit["file_proof"]["uncovered_items"] == []
        assert audit["evidence_needs"] == []
        assert audit["witnesses"][0]["valid"] is True


def test_type1_split_whole_files_prove_both_file_and_boundary(counterexample):
    grouped, separate, items = counterexample
    first = json.dumps({"samples": [items[0]]}, separators=(",", ":")).encode()
    second = json.dumps({"samples": [items[1]]}, separators=(",", ":")).encode()
    witnesses = [
        handoff(first, "handoff-B1", after_anchor="SAMPLE-2"),
        handoff(second, "handoff-B2", before_anchor="RECIPE-FACTORY-V2"),
    ]
    for db in (grouped, separate):
        audit = report.audit(db, witnesses)
        # two anchored originals pin the cut between the samples exactly
        assert audit["file_proof"]["status"] == "complete"
        assert audit["business_proof"]["status"] == "proven"
        assert audit["business_proof"]["value"] == "rework_approval_pending"
        assert audit["business_proof"]["booking_allowed"] is False
        assert len(audit["possible_histories"]) == 1
        assert audit["possible_histories"][0]["submission_boundaries"] == [1]
        assert audit["evidence_needs"] == []


def test_type1_partial_file_can_leave_business_open_while_bytes_are_recovered(three_item_gap):
    db = three_item_gap
    # the tail original (one item) returns with its after anchor: its leading
    # edge is a real cut, but whether the two preceding items shared one
    # submission is still unattested
    tail = json.dumps({"samples": [THREE_ITEMS[2]]},
                      separators=(",", ":")).encode()
    audit = report.audit(db, [handoff(tail, "handoff-tail",
                                      before_anchor="RECIPE-V3")])
    assert audit["file_proof"]["status"] == "partial"
    assert audit["file_proof"]["attested_files"][0]["items"] == ["S3-CLEAR-2"]
    assert set(audit["file_proof"]["uncovered_items"]) == {
        "S3-FLAG", "S3-CLEAR-1"}
    assert audit["business_proof"]["status"] == "indeterminate"
    assert {h["recipe_state"] for h in audit["possible_histories"]} == {
        "approved", "rework_approval_pending"}
    need = next(n for n in audit["evidence_needs"]
                if n["action"] == "retrieve_boundary_manifest")
    assert set(need["target_item_shas"]) == {digest_of(THREE_ITEMS[0]),
                                             digest_of(THREE_ITEMS[1])}
    # only genuinely missing bytes are requested; the attested item is excluded
    file_need = next(n for n in audit["evidence_needs"]
                     if n["action"] == "retrieve_handoff_file")
    assert "S3-CLEAR-2" not in file_need["target_items"]


def test_type1_rejects_saved_as_json_even_though_items_match(counterexample):
    grouped, _separate, items = counterexample
    original = json.dumps({"samples": items}, separators=(",", ":")).encode()
    # the auditor only holds a re-serialized copy plus a claim about the old digest
    saved_as = json.dumps(json.loads(original), indent=2).encode()
    witnesses = [{"type": "handoff_file", "command": "shade-import", "raw": saved_as,
                  "attested_sha": hashlib.sha256(original).hexdigest(),
                  "source": "re-serialized-copy"}]
    audit = report.audit(grouped, witnesses)
    assert audit["witnesses"][0]["valid"] is False
    assert any("digest" in e for e in audit["witnesses"][0]["errors"])
    assert audit["file_proof"]["status"] == "missing"
    assert audit["business_proof"]["status"] == "indeterminate"
    # nor does re-attesting the copy's own digest turn it into the authorized
    # handoff evidence: a save-as file with no surviving anchor position is an
    # unattested source, so it cannot close either the boundary or the file gap
    self_attested = [{"type": "handoff_file", "command": "shade-import",
                      "raw": saved_as,
                      "attested_sha": hashlib.sha256(saved_as).hexdigest(),
                      "source": "self-attested-copy"}]
    audit2 = report.audit(grouped, self_attested)
    assert audit2["file_proof"]["status"] == "missing"
    assert audit2["business_proof"]["status"] == "indeterminate"


def test_type1_item_digest_mismatch_is_rejected(counterexample):
    grouped, _separate, items = counterexample
    tampered = copy.deepcopy(items)
    tampered[0]["delta_e"] = 2.9
    raw = json.dumps({"samples": tampered}, separators=(",", ":")).encode()
    witnesses = [handoff(raw, "tampered-archive",
                         after_anchor="SAMPLE-2", before_anchor="RECIPE-FACTORY-V2")]
    audit = report.audit(grouped, witnesses)
    assert audit["witnesses"][0]["valid"] is False
    assert any("digest" in e for e in audit["witnesses"][0]["errors"])
    assert audit["file_proof"]["status"] == "missing"


def test_type1_wrong_anchor_position_is_rejected(counterexample):
    grouped, _separate, items = counterexample
    raw = json.dumps({"samples": items}, separators=(",", ":")).encode()
    witnesses = [handoff(raw, "misplaced", after_anchor="SAMPLE-1")]
    audit = report.audit(grouped, witnesses)
    assert audit["witnesses"][0]["valid"] is False
    assert any("anchor" in e for e in audit["witnesses"][0]["errors"])


# ---------------------------------------------------------------------------
# 3. Type-2 evidence: signed transaction-boundary manifest, no file bytes
# ---------------------------------------------------------------------------

def test_type2_same_submission_proves_booking_without_file_bytes(counterexample):
    grouped, separate, items = counterexample
    for db in (grouped, separate):
        w = manifest_relation(db, "same", items[0]["id"], items[1]["id"],
                              "receipt-same", before="SAMPLE-2",
                              after="RECIPE-FACTORY-V2")
        audit = report.audit(db, [w])
        assert audit["business_proof"]["status"] == "proven"
        assert audit["business_proof"]["value"] == "approved"
        assert audit["business_proof"]["booking_allowed"] is True
        assert len(audit["possible_histories"]) == 1
        # business is provable while the whole-file proof stays missing
        assert audit["file_proof"]["status"] == "missing"
        needs = [n["action"] for n in audit["evidence_needs"]]
        assert needs == ["retrieve_handoff_file"]
        assert audit["evidence_needs"][0]["target_items"] == [i["id"] for i in items]


def test_type2_adjacent_submissions_proves_customer_signature_required(counterexample):
    grouped, separate, items = counterexample
    for db in (grouped, separate):
        w = manifest_relation(db, "adjacent", items[0]["id"], items[1]["id"],
                              "receipt-adjacent", before="SAMPLE-2",
                              after="RECIPE-FACTORY-V2")
        audit = report.audit(db, [w])
        assert audit["business_proof"]["status"] == "proven"
        assert audit["business_proof"]["value"] == "rework_approval_pending"
        assert audit["business_proof"]["booking_allowed"] is False
        assert len(audit["possible_histories"]) == 1
        assert audit["possible_histories"][0]["lock"]["state"] == "blocked"
        assert audit["file_proof"]["status"] == "missing"


def test_type2_internally_contradictory_manifest_is_rejected(three_item_gap):
    db = three_item_gap
    shas = sha_map(db)
    witness = {"type": "boundary_manifest", "source": "self-contradictory",
               "manifest": {"relations": [
                   {"mode": "same", "item_sha": shas["S3-FLAG"],
                    "other_item_sha": shas["S3-CLEAR-1"]},
                   {"mode": "adjacent", "item_sha": shas["S3-FLAG"],
                    "other_item_sha": shas["S3-CLEAR-1"]}],
                   "anchors": [{"role": "before", "item_sha": shas["SAMPLE-2"]},
                               {"role": "after", "item_sha": shas["RECIPE-V3"]}]}}
    audit = report.audit(db, [witness])
    assert audit["witnesses"][0]["valid"] is False
    assert any("same-submission and adjacent" in e
               for e in audit["witnesses"][0]["errors"])
    assert audit["business_proof"]["status"] == "indeterminate"


def test_type2_multi_relation_manifest_fully_determines_three_item_gap(three_item_gap):
    db = three_item_gap
    shas = sha_map(db)
    witness = {"type": "boundary_manifest", "source": "full-receipt",
               "manifest": {"relations": [
                   {"mode": "adjacent", "item_sha": shas["S3-FLAG"],
                    "other_item_sha": shas["S3-CLEAR-1"]},
                   {"mode": "same", "item_sha": shas["S3-CLEAR-1"],
                    "other_item_sha": shas["S3-CLEAR-2"]}],
                   "anchors": [{"role": "before", "item_sha": shas["SAMPLE-2"]},
                               {"role": "after", "item_sha": shas["RECIPE-V3"]}]}}
    audit = report.audit(db, [witness])
    assert audit["business_proof"]["status"] == "proven"
    assert audit["business_proof"]["value"] == "rework_approval_pending"
    assert audit["possible_histories"][0]["groups"] == [
        ["S3-FLAG"], ["S3-CLEAR-1", "S3-CLEAR-2"]]
    # boundary-only evidence still cannot fabricate the whole-file journal
    assert audit["file_proof"]["status"] == "missing"


def test_type2_rejects_unknown_digest_and_bad_anchor(counterexample):
    grouped, _separate, items = counterexample
    shas = sha_map(grouped)
    forged = {"type": "boundary_manifest", "source": "forged", "manifest": {
        "relations": [{"mode": "same", "item_sha": "deadbeef" * 8,
                       "other_item_sha": shas[items[1]["id"]]}],
        "anchors": []}}
    audit = report.audit(grouped, [forged])
    assert audit["witnesses"][0]["valid"] is False
    bad_anchor = manifest_relation(grouped, "same", items[0]["id"], items[1]["id"],
                                   "bad-anchor", before="SAMPLE-1",
                                   after="RECIPE-FACTORY-V2")
    audit2 = report.audit(grouped, [bad_anchor])
    assert audit2["witnesses"][0]["valid"] is False
    assert any("anchor" in e for e in audit2["witnesses"][0]["errors"])


# ---------------------------------------------------------------------------
# 4. Partial boundary recovery with three gap items
# ---------------------------------------------------------------------------

THREE_ITEMS = [
    {"id": "S3-FLAG", "order_id": "ORDER-1", "instrument_id": "METER-1",
     "at": "2026-01-17", "delta_e": 3.0, "replicate": 1},
    {"id": "S3-CLEAR-1", "order_id": "ORDER-1", "instrument_id": "METER-1",
     "at": "2026-01-18", "delta_e": 1.0, "replicate": 1},
    {"id": "S3-CLEAR-2", "order_id": "ORDER-1", "instrument_id": "METER-1",
     "at": "2026-01-18", "delta_e": 1.1, "replicate": 2},
]


@pytest.fixture
def three_item_gap():
    db = build_gap_db(split=True, items=THREE_ITEMS, followup_id="RECIPE-V3")
    yield db
    db.close()


def test_three_item_gap_has_four_legal_histories(three_item_gap):
    audit = report.audit(three_item_gap)
    states = {h["id"]: h["recipe_state"] for h in audit["possible_histories"]}
    assert len(states) == 4
    assert set(states.values()) == {"approved", "rework_approval_pending"}
    assert audit["business_proof"]["status"] == "indeterminate"


def test_partial_manifest_same_proves_even_with_unknown_remaining_grouping(three_item_gap):
    db = three_item_gap
    w = manifest_relation(db, "same", "S3-FLAG", "S3-CLEAR-1", "partial-same",
                          before="SAMPLE-2", after="RECIPE-V3")
    audit = report.audit(db, [w])
    # every history consistent with the partial manifest gives the same decision
    assert audit["business_proof"]["status"] == "proven"
    assert audit["business_proof"]["value"] == "approved"
    assert audit["business_proof"]["booking_allowed"] is True
    assert len(audit["possible_histories"]) == 2
    assert all(h["recipe_state"] == "approved" for h in audit["possible_histories"])
    # the rest of the boundary and all original bytes remain open
    assert audit["file_proof"]["status"] == "missing"


def test_partial_manifest_adjacent_proves_rework_pending(three_item_gap):
    db = three_item_gap
    w = manifest_relation(db, "adjacent", "S3-FLAG", "S3-CLEAR-1",
                          "partial-adjacent", before="SAMPLE-2", after="RECIPE-V3")
    audit = report.audit(db, [w])
    assert audit["business_proof"]["status"] == "proven"
    assert audit["business_proof"]["value"] == "rework_approval_pending"
    assert audit["business_proof"]["booking_allowed"] is False
    assert all(h["recipe_state"] == "rework_approval_pending"
               for h in audit["possible_histories"])


def test_partial_handoff_file_records_real_file_while_boundary_stays_open(three_item_gap):
    db = three_item_gap
    # single tail item: the file itself is real evidence, but the cut between
    # the two head items remains unknown, so the business cannot be settled
    tail = json.dumps({"samples": [THREE_ITEMS[2]]},
                      separators=(",", ":")).encode()
    audit = report.audit(db, [handoff(tail, "partial-file",
                                      before_anchor="RECIPE-V3")])
    assert audit["file_proof"]["status"] == "partial"
    assert audit["file_proof"]["attested_files"][0]["items"] == ["S3-CLEAR-2"]
    assert set(audit["file_proof"]["uncovered_items"]) == {
        "S3-FLAG", "S3-CLEAR-1"}
    assert audit["business_proof"]["status"] == "indeterminate"
    # next file retrieval never re-requests the attested item
    file_need = next(n for n in audit["evidence_needs"]
                     if n["action"] == "retrieve_handoff_file")
    assert "S3-CLEAR-2" not in file_need["target_items"]


# ---------------------------------------------------------------------------
# 5. Two independent valid originals that contradict each other
# ---------------------------------------------------------------------------

def test_conflicting_manifests_are_reported_not_resolved_by_current_state(counterexample):
    grouped, _separate, items = counterexample
    witnesses = [
        manifest_relation(grouped, "same", items[0]["id"], items[1]["id"],
                          "receipt-same", before="SAMPLE-2", after="RECIPE-FACTORY-V2"),
        manifest_relation(grouped, "adjacent", items[0]["id"], items[1]["id"],
                          "receipt-adjacent", before="SAMPLE-2",
                          after="RECIPE-FACTORY-V2"),
    ]
    audit = report.audit(grouped, witnesses)
    assert all(w["valid"] for w in audit["witnesses"])
    assert audit["business_proof"]["status"] == "contradicted"
    assert audit["business_proof"]["booking_allowed"] is None
    assert audit["file_proof"]["status"] == "contradicted"
    conflict = audit["business_proof"]["contradictions"][0]
    assert {conflict["witness_a"], conflict["witness_b"]} == {
        "receipt-same", "receipt-adjacent"}
    # the only suggested action targets the disputed originals, never a re-query
    assert audit["evidence_needs"][0]["action"] == "retrieve_handoff_file"
    assert set(audit["evidence_needs"][0]["target_items"]) == {i["id"] for i in items}


def test_conflicting_handoff_files_are_contradictory_witnesses(counterexample):
    grouped, _separate, items = counterexample
    grouped_raw = json.dumps({"samples": items}, separators=(",", ":")).encode()
    split_raw = json.dumps({"samples": [items[0]]}, separators=(",", ":")).encode()
    witnesses = [
        handoff(grouped_raw, "archive-grouped",
                after_anchor="SAMPLE-2", before_anchor="RECIPE-FACTORY-V2"),
        handoff(split_raw, "archive-split", after_anchor="SAMPLE-2"),
    ]
    audit = report.audit(grouped, witnesses)
    assert all(w["valid"] for w in audit["witnesses"])
    assert audit["business_proof"]["status"] == "contradicted"
    assert audit["file_proof"]["status"] == "contradicted"


# ---------------------------------------------------------------------------
# 6. Backward compatibility and scope limits
# ---------------------------------------------------------------------------

def test_independent_consistent_receipts_combine_into_one_history(three_item_gap):
    db = three_item_gap
    shas = sha_map(db)
    receipt_a = {"type": "boundary_manifest", "source": "receipt-a", "manifest": {
        "relations": [{"mode": "same", "item_sha": shas["S3-FLAG"],
                       "other_item_sha": shas["S3-CLEAR-1"]}],
        "anchors": [{"role": "before", "item_sha": shas["SAMPLE-2"]},
                    {"role": "after", "item_sha": shas["RECIPE-V3"]}]}}
    receipt_b = {"type": "boundary_manifest", "source": "receipt-b", "manifest": {
        "relations": [{"mode": "adjacent", "item_sha": shas["S3-CLEAR-1"],
                       "other_item_sha": shas["S3-CLEAR-2"]}],
        "anchors": [{"role": "before", "item_sha": shas["SAMPLE-2"]},
                    {"role": "after", "item_sha": shas["RECIPE-V3"]}]}}
    audit = report.audit(db, [receipt_a, receipt_b])
    assert all(w["valid"] for w in audit["witnesses"])
    assert "contradictions" not in audit["business_proof"]
    # same(X,Y) forbids the p1 cut; adjacent(Y,Z) requires the p2 cut
    assert len(audit["possible_histories"]) == 1
    assert audit["possible_histories"][0]["groups"] == [
        ["S3-FLAG", "S3-CLEAR-1"], ["S3-CLEAR-2"]]
    assert audit["business_proof"]["status"] == "proven"


def test_healthy_journal_keeps_legacy_audit_shape():
    db = connect(":memory:")
    try:
        seed_anchor_files(db)
        assert report.audit(db) == {"ok": True, "files": 3}
    finally:
        db.close()


def test_audit_never_accepts_current_projection_as_witness(counterexample):
    grouped, _separate, _items = counterexample
    # the decision table and a past audit output are not admissible originals
    projection = [dict(r) for r in grouped.execute("SELECT * FROM recipe_decision")]
    bogus = {"type": "boundary_manifest", "source": "current-projection",
             "manifest": projection}
    audit = report.audit(grouped, [bogus])
    assert audit["witnesses"][0]["valid"] is False
    assert audit["business_proof"]["status"] == "indeterminate"


def test_evidence_needs_never_re_requests_attested_items(counterexample):
    grouped, _separate, items = counterexample
    first = json.dumps({"samples": [items[0]]}, separators=(",", ":")).encode()
    audit = report.audit(grouped, [handoff(first, "B1", after_anchor="SAMPLE-2")])
    for need in audit["evidence_needs"]:
        targets = need.get("target_items") or []
        assert items[0]["id"] not in targets


def test_duplicate_attested_originals_are_corroboration_not_contradiction(three_item_gap):
    db = three_item_gap
    raw = json.dumps({"samples": THREE_ITEMS[:2]}, separators=(",", ":")).encode()
    witnesses = [
        handoff(raw, "archive-copy-1", after_anchor="SAMPLE-2"),
        handoff(raw, "archive-copy-2", after_anchor="SAMPLE-2"),
    ]
    audit = report.audit(db, witnesses)
    # two identical attested copies corroborate one another; every legal
    # history keeps the first two samples grouped, so the business is settled
    assert audit["business_proof"]["status"] == "proven"
    assert audit["business_proof"]["value"] == "approved"
    assert "contradictions" not in audit["business_proof"]
    assert audit["file_proof"]["status"] == "partial"
    assert len(audit["file_proof"]["attested_files"]) == 2


def test_cumulative_partial_file_and_manifest_settle_business_not_bytes(three_item_gap):
    db = three_item_gap
    first = json.dumps({"samples": [THREE_ITEMS[0]]},
                       separators=(",", ":")).encode()
    manifest = manifest_relation(db, "same", "S3-CLEAR-1", "S3-CLEAR-2",
                                 "tail-receipt", before="SAMPLE-2",
                                 after="RECIPE-V3")
    audit = report.audit(db, [handoff(first, "B1", after_anchor="SAMPLE-2"),
                              manifest])
    # the head original pins X into its own submission, so the rework lock was
    # set; the signed receipt then fully determines the remaining Y|Z grouping.
    # Business is proven even though the tail whole-file bytes are still missing.
    assert audit["business_proof"]["status"] == "proven"
    assert audit["business_proof"]["value"] == "rework_approval_pending"
    assert audit["business_proof"]["booking_allowed"] is False
    assert len(audit["possible_histories"]) == 1
    assert audit["possible_histories"][0]["groups"] == [
        ["S3-FLAG"], ["S3-CLEAR-1", "S3-CLEAR-2"]]
    assert audit["file_proof"]["status"] == "partial"
    assert set(audit["file_proof"]["uncovered_items"]) == {
        "S3-CLEAR-1", "S3-CLEAR-2"}
    # no boundary retrieval is needed any more; only missing bytes remain
    assert all(n["action"] == "retrieve_handoff_file"
               for n in audit["evidence_needs"])


def test_record_and_file_record_remain_distinct_objects():
    db = connect(":memory:")
    try:
        item = THREE_ITEMS[0]
        assert record(db, "sample", item) is True
        assert record(db, "sample", item) is False  # identity replay is idempotent
        raw = json.dumps({"samples": [item]}).encode()
        with db:
            file_record(db, "shade-import", raw)
        ev = db.execute("SELECT raw FROM evidence WHERE id=?", (item["id"],)).fetchone()
        fj = db.execute("SELECT raw FROM file_journal").fetchone()
        assert ev["raw"] != fj["raw"]  # normalized item vs whole file bytes
        assert db.execute("SELECT sha FROM evidence WHERE id=?",
                          (item["id"],)).fetchone()["sha"] == digest_of(item)
    finally:
        db.close()
