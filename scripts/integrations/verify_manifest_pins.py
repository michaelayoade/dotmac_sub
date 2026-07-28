"""Fail deployment when an enabled installation pin is not executable.

The check runs from the candidate image against the target database before
customer traffic moves. Supported historical definitions are reported as
adoption debt but remain executable during their bounded rollback window.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from app.db import SessionLocal
from app.services.integrations import installations


def manifest_pin_report(
    checks: Sequence[installations.ManifestPinCheck],
) -> dict[str, object]:
    unavailable = [
        check
        for check in checks
        if check.state is installations.ManifestPinState.unavailable
    ]
    historical = [
        check
        for check in checks
        if check.state is installations.ManifestPinState.supported_historical
    ]
    return {
        "ok": not unavailable,
        "checked_installation_count": len(checks),
        "unavailable_count": len(unavailable),
        "supported_historical_count": len(historical),
        "installations": [
            {
                "installation_id": str(check.installation_id),
                "connector_key": check.connector_key,
                "installation_state": check.installation_state,
                "pin_state": check.state.value,
                "installed_connector_version": (check.installed_pin.connector_version),
                "installed_manifest_digest": check.installed_pin.manifest_digest,
                "deployed_connector_version": (
                    check.deployed_pin.connector_version if check.deployed_pin else None
                ),
                "deployed_manifest_digest": (
                    check.deployed_pin.manifest_digest if check.deployed_pin else None
                ),
            }
            for check in checks
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that integration installation manifest pins are executable "
            "by this application image."
        )
    )
    parser.add_argument(
        "--all-non-retired",
        action="store_true",
        help="report every non-retired installation instead of enabled only",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        checks = installations.list_manifest_pin_checks(
            db,
            enabled_only=not args.all_non_retired,
        )
        report = manifest_pin_report(checks)
    finally:
        db.close()
    print(json.dumps(report, sort_keys=True))
    return 0 if bool(report["ok"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
