import json
from pathlib import Path

from dyebatch import legacy_capture, recipe, shade, vat
from dyebatch.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_legacy_register_interpretation_changes_actual_shade_outcome():
    for value, expected in ((14, "released"), (25, "rework")):
        db = connect(":memory:")
        try:
            recipe.import_data(db, read("recipe_ok.json"))
            vat.import_data(db, read("vat_ok.json"))
            shade.import_data(db, read("shade_ok.json"))
            envelope = read("legacy-register.json")
            envelope["records"][0]["register"] = value
            assert legacy_capture.import_data(db, envelope)[0]["state"] == expected
            recorded = db.execute("SELECT count(*) FROM evidence WHERE kind='legacy_register'").fetchone()
            assert recorded[0] == 1
        finally:
            db.close()
