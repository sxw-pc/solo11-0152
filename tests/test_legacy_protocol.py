"""Acceptance for the protocol-gated legacy register capture.

Covers: conditional decoding of the shared code stream, post-page
accumulation state, one use per evidence route, redundant diagnostics
not consuming the opportunity, identity-conflict rejection, report
replay, unique-truth import without fabrication, and restart rounds.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from dyebatch import legacy_capture, recipe, shade, vat
from dyebatch.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def base_db():
    db = connect(":memory:")
    recipe.import_data(db, read("recipe_ok.json"))
    vat.import_data(db, read("vat_ok.json"))
    shade.import_data(db, read("shade_ok.json"))
    return db


def envelope():
    return read("legacy-register.json")


def archive(scheme, **overrides):
    doc = {
        "id": "ARCH-Q1",
        "session": "Q1",
        "device_config": {"model": "CM-legacy", "firmware": "4.2", "calibration": "bench"},
        "layout_range": {"file": "legacy-register.json", "pages": [1, 2]},
        "format_header": {"scheme": scheme, "round": 1, "page": "original"},
    }
    doc.update(overrides)
    return doc


def diagnostic(observed, seed=10, test=4, **overrides):
    doc = {
        "id": "DIAG-Q1",
        "session": "Q1",
        "probe": {"seed": seed, "test": test},
        "observed": observed,
    }
    doc.update(overrides)
    return doc


def shade_rows(db):
    return {r["id"]: r["delta_e"] for r in db.execute("SELECT id, delta_e FROM shade_sample")}


def provenance(db, sample_id):
    raw = db.execute("SELECT raw FROM evidence WHERE id=?", (sample_id,)).fetchone()["raw"]
    return json.loads(raw)["provenance"]


def restart_segment():
    return {
        "records": [
            {
                "id": "RAW-7",
                "order_id": "ORDER-1",
                "instrument_id": "METER-1",
                "at": "2026-01-18",
                "replicate": 1,
                "register": 3,
                "block": "Q1",
            }
        ],
        "register_file_excerpt": {
            "legacy_session": "Q1",
            "restart": True,
            "anchors": [{"record": "restart-readout", "value": 20}],
            "raw_sequence": [3],
            "schemes": ["absolute-sample", "signed-differences"],
            "safe_bench_probes": [{"seed": 10, "test": 4}],
            "format_not_claimed": True,
        },
    }


def test_shared_code_stream_decodes_only_with_certified_protocol():
    for scheme, delta_e, verdict in (
        ("absolute-sample", 1.4, "released"),
        ("signed-differences", 2.5, "rework"),
    ):
        db = base_db()
        try:
            held = legacy_capture.import_data(db, envelope())
            assert held["imported"] == []
            record = held["pending"][0]["records"][0]
            assert record["record"] == "RAW-1"
            assert record["candidates"] == {"absolute-sample": 1.4, "signed-differences": 2.5}
            assert "RAW-1-shade" not in shade_rows(db)

            settled = legacy_capture.apply_archive(db, archive(scheme))
            assert settled["certified"] is True
            assert settled["format"] == scheme
            assert settled["imported"][0]["state"] == verdict
            assert settled["pending"] == []
            assert shade_rows(db)["RAW-1-shade"] == delta_e

            excerpt_id = db.execute(
                "SELECT excerpt_id FROM register_round WHERE session='Q1'"
            ).fetchone()[0]
            prov = provenance(db, "RAW-1-shade")
            assert prov["source_file"] == excerpt_id
            assert prov["source_record"] == "RAW-1"
            assert prov["protocol_evidence"] == "ARCH-Q1"
            assert prov["scheme"] == scheme
            assert prov["raw_word"] == 14
            assert prov["decoded_register"] == (14 if scheme == "absolute-sample" else 25)
            assert prov["base_value"] == (None if scheme == "absolute-sample" else 11)
        finally:
            db.close()


def test_page_break_never_resets_the_accumulation():
    for scheme, second_value in (("absolute-sample", 0.1), ("signed-differences", 2.6)):
        db = base_db()
        try:
            env = envelope()
            env["records"].append(
                {
                    "id": "RAW-2",
                    "order_id": "ORDER-1",
                    "instrument_id": "METER-1",
                    "at": "2026-01-17",
                    "replicate": 2,
                    "register": 1,
                    "block": "Q1",
                }
            )
            legacy_capture.import_data(db, env)
            legacy_capture.apply_archive(db, archive(scheme))
            rows = shade_rows(db)
            assert rows["RAW-2-shade"] == second_value
            page, base = db.execute(
                "SELECT page, base FROM register_word WHERE record_id='RAW-2'"
            ).fetchone()
            assert page == 2
            if scheme == "signed-differences":
                # page 2 continues from 25; a reset to zero would read 0.1
                assert rows["RAW-1-shade"] == 2.5
                assert base == 25
                assert provenance(db, "RAW-2-shade")["base_value"] == 25
        finally:
            db.close()


def test_each_evidence_route_is_usable_once_per_round():
    db = base_db()
    try:
        legacy_capture.import_data(db, envelope())
        thin = {"id": "ARCH-MODEL-ONLY", "session": "Q1", "device_model": "CM-legacy"}
        out = legacy_capture.apply_archive(db, thin)
        assert out["certified"] is False
        assert "version connection" in out["reason"]
        # the archive opportunity for this round is spent even though it proved nothing
        with pytest.raises(ValueError):
            legacy_capture.apply_archive(db, archive("absolute-sample", id="ARCH-SECOND"))
        # the diagnostic route is still open and settles the round
        settled = legacy_capture.apply_diagnostic(db, diagnostic(-6))
        assert settled["certified"] is True
        assert settled["format"] == "signed-differences"
        # a conflicting second diagnostic is rejected; a consistent one is a no-op
        with pytest.raises(ValueError):
            legacy_capture.apply_diagnostic(db, diagnostic(4, id="DIAG-SECOND"))
        again = legacy_capture.apply_diagnostic(db, diagnostic(-6, id="DIAG-CONFIRM"))
        assert again["redundant"] is True
    finally:
        db.close()


def test_adjacent_page_header_has_no_version_connection():
    db = base_db()
    try:
        legacy_capture.import_data(db, envelope())
        copied = archive("absolute-sample")
        copied["format_header"]["page"] = "adjacent-copy"
        out = legacy_capture.apply_archive(db, copied)
        assert out["certified"] is False
        assert "adjacent page" in out["reason"]
        assert db.execute("SELECT format FROM register_round").fetchone()[0] is None
        assert "RAW-1-shade" not in shade_rows(db)
    finally:
        db.close()


def test_redundant_diagnostic_does_not_consume_the_opportunity():
    db = base_db()
    try:
        legacy_capture.import_data(db, envelope())
        blind = legacy_capture.apply_diagnostic(db, diagnostic(4, seed=0, test=4, id="DIAG-BLIND"))
        assert blind["redundant"] is True
        assert blind["certified"] is False
        row = db.execute("SELECT format, diagnostic_used FROM register_round").fetchone()
        assert tuple(row) == (None, 0)
        # an impossible reading is rejected outright
        with pytest.raises(ValueError):
            legacy_capture.apply_diagnostic(db, diagnostic(5, seed=0, test=4, id="DIAG-WRONG"))
        # the decisive probe is still available afterwards
        settled = legacy_capture.apply_diagnostic(db, diagnostic(-6, id="DIAG-DECISIVE"))
        assert settled["format"] == "signed-differences"
        assert shade_rows(db)["RAW-1-shade"] == 2.5
    finally:
        db.close()


def test_probe_must_be_sanctioned_for_the_round():
    db = base_db()
    try:
        legacy_capture.import_data(db, envelope())
        with pytest.raises(ValueError):
            legacy_capture.apply_diagnostic(db, diagnostic(1, seed=3, test=4))
    finally:
        db.close()


def test_same_original_with_different_text_is_rejected():
    db = base_db()
    try:
        legacy_capture.import_data(db, envelope())
        before = db.execute("SELECT count(*) FROM evidence").fetchone()[0]
        altered = envelope()
        altered["records"][0]["order_id"] = "ORDER-9"
        with pytest.raises(ValueError):
            legacy_capture.import_data(db, altered)
        assert db.execute("SELECT count(*) FROM evidence").fetchone()[0] == before

        legacy_capture.apply_archive(db, archive("absolute-sample"))
        with pytest.raises(ValueError):
            legacy_capture.apply_archive(db, archive("signed-differences"))

        legacy_capture.apply_diagnostic(db, diagnostic(4, seed=0, test=4, id="DIAG-X"))
        with pytest.raises(ValueError):
            legacy_capture.apply_diagnostic(db, diagnostic(-6, id="DIAG-X"))

        # an identical re-import is accepted and enters nothing twice
        again = legacy_capture.import_data(db, envelope())
        assert again["imported"] == []
        assert again["pending"] == []
        assert db.execute("SELECT count(*) FROM shade_sample").fetchone()[0] == 3
    finally:
        db.close()


def test_unique_truth_imports_but_split_truth_stays_pending():
    # anchor 0 makes word 14 decode to 14 under both formats: importable
    db = base_db()
    try:
        env = envelope()
        env["register_file_excerpt"]["anchors"] = [{"record": "last-absolute", "value": 0}]
        env["register_file_excerpt"]["raw_sequence"] = [14]
        result = legacy_capture.import_data(db, env)
        assert result["pending"] == []
        assert shade_rows(db)["RAW-1-shade"] == 1.4
        prov = provenance(db, "RAW-1-shade")
        assert prov["scheme"] == "unique-across-schemes"
        assert prov["protocol_evidence"].startswith("Q1-excerpt-")
        assert prov["source_record"] == "RAW-1"
    finally:
        db.close()

    # anchor 5 with word 1: 0.1 vs 0.6 both pass the customer threshold,
    # but the true value differs, so no row may be fabricated
    db = base_db()
    try:
        env = envelope()
        env["records"][0]["register"] = 1
        env["register_file_excerpt"]["anchors"] = [{"record": "last-absolute", "value": 5}]
        env["register_file_excerpt"]["raw_sequence"] = [1]
        result = legacy_capture.import_data(db, env)
        assert result["pending"][0]["records"][0]["candidates"] == {
            "absolute-sample": 0.1,
            "signed-differences": 0.6,
        }
        assert "RAW-1-shade" not in shade_rows(db)
        assert db.execute("SELECT state FROM shade_release").fetchone()[0] == "released"
    finally:
        db.close()


def test_restart_anchor_opens_a_new_round_without_reentering_stable_records():
    db = base_db()
    try:
        legacy_capture.import_data(db, envelope())
        legacy_capture.apply_archive(db, archive("absolute-sample"))
        assert shade_rows(db)["RAW-1-shade"] == 1.4
        stable_evidence = db.execute("SELECT count(*) FROM evidence").fetchone()[0]

        second = legacy_capture.import_data(db, restart_segment())
        # the round-1 certificate does not leak across the restart
        assert second["pending"][0]["round"] == 2
        assert second["pending"][0]["records"][0]["candidates"] == {
            "absolute-sample": 0.3,
            "signed-differences": 2.3,
        }
        # the stable segment is not entered again
        assert db.execute("SELECT count(*) FROM evidence").fetchone()[0] == stable_evidence + 2
        assert db.execute("SELECT count(*) FROM shade_sample").fetchone()[0] == 3

        # the live round has fresh evidence routes and decodes from the new anchor
        settled = legacy_capture.apply_diagnostic(db, diagnostic(-6, id="DIAG-R2"))
        assert settled["round"] == 2
        assert settled["format"] == "signed-differences"
        assert shade_rows(db)["RAW-7-shade"] == 2.3
        assert provenance(db, "RAW-7-shade")["base_value"] == 20
        assert shade_rows(db)["RAW-1-shade"] == 1.4
    finally:
        db.close()


def test_continuation_without_restart_anchor_is_rejected():
    db = base_db()
    try:
        legacy_capture.import_data(db, envelope())
        forged = envelope()
        forged["records"] = [
            {
                "id": "RAW-9",
                "order_id": "ORDER-1",
                "instrument_id": "METER-1",
                "at": "2026-01-18",
                "replicate": 1,
                "register": 7,
                "block": "Q1",
            }
        ]
        forged["register_file_excerpt"]["raw_sequence"] = [7]
        with pytest.raises(ValueError):
            legacy_capture.import_data(db, forged)
    finally:
        db.close()


def test_diagnostic_cannot_certify_a_pre_restart_round():
    db = base_db()
    try:
        legacy_capture.import_data(db, envelope())
        legacy_capture.import_data(db, restart_segment())
        settled = legacy_capture.apply_diagnostic(db, diagnostic(-6, id="DIAG-LIVE"))
        assert settled["round"] == 2
        rows = shade_rows(db)
        assert "RAW-7-shade" in rows
        assert "RAW-1-shade" not in rows
        # the archive route still recovers the old round independently
        out = legacy_capture.apply_archive(db, archive("absolute-sample"))
        assert out["certified"] is True
        assert shade_rows(db)["RAW-1-shade"] == 1.4
    finally:
        db.close()


def test_plan_offers_one_decisive_probe_and_no_blind_trial():
    db = base_db()
    try:
        plan = legacy_capture.import_data(db, envelope())["plan"]
        assert "one new evidence source" in plan["budget"]
        actions = {a["action"]: a for a in plan["actions"]}

        arch = actions["factory_archive"]
        assert arch["round"] == 1
        by_scheme = {
            o["return"]["format_header"]["scheme"]: o["decodes"]
            for o in arch["outcomes"]
            if "format_header" in o["return"]
        }
        assert by_scheme["absolute-sample"] == {"RAW-1": 1.4}
        assert by_scheme["signed-differences"] == {"RAW-1": 2.5}
        assert any(o["decodes"] == {} for o in arch["outcomes"])

        diag = actions["online_diagnostic"]
        assert diag["probe"] == {"seed": 10, "test": 4}
        by_observed = {o["observed"]: o for o in diag["outcomes"]}
        assert by_observed[4]["certifies"] == "absolute-sample"
        assert by_observed[4]["decodes"] == {"RAW-1": 1.4}
        assert by_observed[-6]["certifies"] == "signed-differences"
        assert by_observed[-6]["decodes"] == {"RAW-1": 2.5}
        assert diag["redundant_probes"][0]["probe"] == {"seed": 0, "test": 4}
    finally:
        db.close()

    # when only the seed-0 probe exists the plan must not demand a blind trial
    db = base_db()
    try:
        env = envelope()
        env["register_file_excerpt"]["safe_bench_probes"] = [{"seed": 0, "test": 4}]
        plan = legacy_capture.import_data(db, env)["plan"]
        diag = {a["action"]: a for a in plan["actions"]}["online_diagnostic"]
        assert diag["decisive"] is False
        assert "blind" in diag["note"]
    finally:
        db.close()


def test_register_capture_replays_through_report_audit():
    for evidence_name, evidence_doc, expected in (
        ("archive.json", archive("absolute-sample"), "released"),
        ("diagnostic.json", diagnostic(-6), "rework"),
    ):
        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "state.sqlite")
            evidence_path = Path(d) / evidence_name
            evidence_path.write_text(json.dumps(evidence_doc), encoding="utf-8")
            command = "register-archive" if "format_header" in evidence_doc else "register-diagnose"

            def run(*args):
                r = subprocess.run(
                    [sys.executable, "-B", "-m", "dyebatch", *args, "--db", db_path],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                assert r.returncode == 0, r.stderr
                return json.loads(r.stdout)

            run("recipe-import", "--input", str(FIXTURES / "recipe_ok.json"))
            run("vat-plan", "--input", str(FIXTURES / "vat_ok.json"))
            run("shade-import", "--input", str(FIXTURES / "shade_ok.json"))
            held = run("register-import", "--input", str(FIXTURES / "legacy-register.json"))
            assert held["pending"][0]["records"][0]["record"] == "RAW-1"
            assert run("report")[0]["shade"]["state"] == "released"
            settled = run(command, "--input", str(evidence_path))
            assert settled["certified"] is True
            assert run("report")[0]["shade"]["state"] == expected
            assert run("audit")["ok"] is True
