"""Read-only Payments intent floor gate before restoring a legacy image.

Exit zero only when the existing runtime login proves the complete predecessor
shape and no installed Payments intent correlation floor. No business rows or
activation flags participate in this decision.
"""

from __future__ import annotations

import json
import sys

from app.db import SessionLocal, begin_read_only_snapshot
from app.services.payments_intent_schema_floor import (
    PaymentsIntentSchemaState,
    classify_payments_intent_schema_floor,
    legacy_image_rollback_allowed,
    read_payments_intent_schema_floor,
)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    inspect_mode = argv == ["inspect"]
    if argv and not inspect_mode:
        print("PAYMENTS INTENT ROLLBACK REFUSED: unexpected arguments", file=sys.stderr)
        return 2
    try:
        with SessionLocal() as db:
            begin_read_only_snapshot(db)
            observation = read_payments_intent_schema_floor(db)
            floor = classify_payments_intent_schema_floor(observation)
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
        # Driver errors can contain a database URL; never forward them.
        print(
            "PAYMENTS INTENT ROLLBACK REFUSED: catalog proof unavailable",
            file=sys.stderr,
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
                        "correlation_columns": observation.correlation_columns,
                        "correlation_constraints": observation.correlation_constraints,
                        "correlation_triggers": observation.correlation_triggers,
                        "correlation_functions": observation.correlation_functions,
                        "installed_shape_complete": observation.installed_shape_complete,
                    },
                },
                sort_keys=True,
            )
        )
        return 0 if floor is not PaymentsIntentSchemaState.UNKNOWN else 2
    if not allowed:
        print(
            "PAYMENTS INTENT ROLLBACK REFUSED: installed or unproved floor",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
