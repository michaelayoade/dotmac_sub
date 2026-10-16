"""Read-only candidate-image gate before restoring an older application image.

Exit 0 means the database still has the proved pre-installation legacy shape.
Every other result forbids automatic image rollback. This script never writes.
"""

from __future__ import annotations

import json
import sys

from app.db import SessionLocal, begin_read_only_snapshot
from app.services.payment_email_cutover import (
    PaymentEmailSchemaState,
    classify_payment_email_schema_floor,
    legacy_image_rollback_allowed,
    read_legacy_image_rollback_floor,
)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    inspect_mode = argv == ["inspect"]
    if argv and not inspect_mode:
        print("PAYMENT EMAIL ROLLBACK REFUSED: unexpected arguments", file=sys.stderr)
        return 2
    try:
        with SessionLocal() as db:
            begin_read_only_snapshot(db)
            observation = read_legacy_image_rollback_floor(db)
            floor = classify_payment_email_schema_floor(observation)
            allowed = legacy_image_rollback_allowed(observation)
            db.rollback()
    except Exception:
        if inspect_mode:
            print(
                json.dumps(
                    {"schema_version": 1, "floor": "unknown", "observation": None}
                )
            )
            return 2
        # Deliberately omit driver messages: they may contain a connection URL.
        print(
            "PAYMENT EMAIL ROLLBACK REFUSED: catalog proof unavailable", file=sys.stderr
        )
        return 2
    if inspect_mode:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "floor": floor.value,
                    "observation": {
                        "legacy_shape_complete": observation.legacy_shape_complete,
                        "installed_shape_complete": observation.installed_shape_complete,
                        "installation_relations": sum(
                            (
                                observation.cutover_relation,
                                observation.episode_relation,
                                observation.part_relation,
                            )
                        ),
                        "seal_columns": int(observation.seal_column),
                        "guard_triggers": observation.guard_triggers,
                        "guard_functions": observation.guard_functions,
                    },
                },
                sort_keys=True,
            )
        )
        return 0 if floor is not PaymentEmailSchemaState.UNKNOWN else 2
    if not allowed:
        print(
            "PAYMENT EMAIL ROLLBACK REFUSED: installed composition floor",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
