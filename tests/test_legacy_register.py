import json
from pathlib import Path

from dyebatch import legacy_capture, recipe, shade, vat
from dyebatch.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_legacy_register_interpretation_changes_actual_shade_outcome():
    for scheme, expected in (("absolute-sample", "released"), ("signed-differences", "rework")):
        db = connect(":memory:")
        try:
            recipe.import_data(db, read("recipe_ok.json"))
            vat.import_data(db, read("vat_ok.json"))
            shade.import_data(db, read("shade_ok.json"))
            envelope = read("legacy-register.json")
            held = legacy_capture.import_data(db, envelope)
            # the protocol field is illegible, so the raw word stays out of
            # the measurement ledger until a certificate arrives
            assert held["imported"] == []
            assert held["pending"][0]["records"][0]["record"] == "RAW-1"
            archive = {
                "id": "ARCH-Q1",
                "session": "Q1",
                "device_config": {"model": "CM-legacy", "firmware": "4.2"},
                "layout_range": {"file": "legacy-register.json", "pages": [1, 2]},
                "format_header": {"scheme": scheme, "round": 1, "page": "original"},
            }
            settled = legacy_capture.apply_archive(db, archive)
            assert settled["certified"] is True
            assert settled["imported"][0]["state"] == expected
            recorded = db.execute("SELECT count(*) FROM evidence WHERE kind='legacy_register'").fetchone()
            assert recorded[0] == 1
        finally:
            db.close()
