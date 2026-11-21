import json
from pathlib import Path

from dyebatch import recipe, report, shade, vat
from dyebatch.source import file_record
from dyebatch.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def seed(db, command, handler, data):
    result = handler(db, data)
    with db:
        file_record(db, command, json.dumps(data).encode("utf-8"))
    return result


def reconstruct(*, split):
    db = connect(":memory:")
    try:
        original = read("recipe_ok.json")
        seed(db, "recipe-import", recipe.import_data, original)
        seed(db, "vat-plan", vat.import_data, read("vat_ok.json"))
        seed(db, "shade-import", shade.import_data, read("shade_ok.json"))
        raw = read("audit_missing_shade_boundary.json")

        if split:
            shade.import_data(db, {"samples": [raw["first"]]})
            shade.import_data(db, {"samples": [raw["second"]]})
        else:
            shade.import_data(db, {"samples": [raw["first"], raw["second"]]})

        revision = dict(
            original["revisions"][0], id=raw["followup_id"], version=2, approved_by="factory"
        )
        decision = seed(db, "recipe-import", recipe.import_data, {"revisions": [revision]})[0]
        records = [tuple(row) for row in db.execute("SELECT * FROM evidence ORDER BY rowid")]
        files = [tuple(row) for row in db.execute("SELECT * FROM file_journal ORDER BY id")]
        result = report.audit(db)
        assert result["ok"] is False and result["files"] == 4
        return decision["state"], records, files, result
    finally:
        db.close()


def test_equal_surviving_originals_do_not_prove_import_boundary():
    grouped, grouped_items, grouped_files, grouped_audit = reconstruct(split=False)
    separate, separate_items, separate_files, separate_audit = reconstruct(split=True)
    assert (grouped, separate) == ("approved", "rework_approval_pending")
    assert grouped_items == separate_items
    assert grouped_files == separate_files
    # the enhanced audit is machine-independent: identical surviving evidence
    # yields identical findings, and both legal histories are replayed for real
    assert grouped_audit == separate_audit
    audit = grouped_audit
    assert audit["business_proof"]["status"] == "indeterminate"
    assert set(audit["business_proof"]["candidate_values"]) == {
        "approved", "rework_approval_pending"}
    assert {h["recipe_state"] for h in audit["possible_histories"]} == {
        "approved", "rework_approval_pending"}
    assert audit["file_proof"]["status"] == "missing"
