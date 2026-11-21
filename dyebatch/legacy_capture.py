"""Read a synthetic register export from an older shade instrument."""

from . import shade
from .source import record


def import_data(db, data):
    items = []
    with db:
        for row in data["records"]:
            if record(db, "legacy_register", row):
                items.append(
                    {
                        "id": row["id"] + "-shade",
                        "order_id": row["order_id"],
                        "instrument_id": row["instrument_id"],
                        "at": row["at"],
                        "replicate": row["replicate"],
                        "delta_e": int(row["register"]) / 10,
                    }
                )
        return shade.import_data(db, {"samples": items})
