"""Rollback: resolve every ``network:router-health:`` admin alert (audited).

Dry run by default: reports how many router-health alerts are open and changes
nothing. ``--apply`` resolves them through the owner command (writes an audit
event). Run on a host with application settings, never with a destructive flag
combination; there is no delete.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import func, select

from app.models.admin_alert import AdminAlert
from app.models.network_monitoring import AlertStatus
from app.services.db_session_adapter import db_session_adapter
from app.services.router_health import commands
from app.services.router_health.contracts import FINGERPRINT_ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="resolve the open alerts (default: dry run)",
    )
    args = parser.parse_args(argv)
    db = db_session_adapter.create_session()
    try:
        open_count = db.scalar(
            select(func.count())
            .select_from(AdminAlert)
            .where(
                AdminAlert.fingerprint.like(f"{FINGERPRINT_ROOT}%"),
                AdminAlert.status != AlertStatus.resolved,
            )
        )
        db.rollback()
        print(f"open router-health alerts: {open_count}")
        if not args.apply:
            print("dry run; pass --apply to resolve them")
            return 0
        resolved = commands.retire_router_health_findings(
            db,
            command=commands.RetireFindingsCommand(
                context=commands.system_context("scripts.router_health_retire_findings")
            ),
        )
        print(f"resolved: {resolved}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
