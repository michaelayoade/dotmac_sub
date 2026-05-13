#!/usr/bin/env python
"""Import live OLT profile state into DB source-of-truth tables.

Usage:
    poetry run python scripts/import_olt_state.py --olt-id <uuid>
    poetry run python scripts/import_olt_state.py --olt-name boi-olt
    poetry run python scripts/import_olt_state.py --all
    poetry run python scripts/import_olt_state.py --all --dump-root /root/olt_audit_20260506
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app.models.network import OLTDevice
from app.services.network.olt_state_import import (
    import_olt_state,
    import_olt_state_from_dump,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _selected_olts(db, args) -> list[OLTDevice]:
    if args.olt_id:
        olt = db.get(OLTDevice, args.olt_id)
        return [olt] if olt else []
    if args.olt_name:
        return list(
            db.scalars(select(OLTDevice).where(OLTDevice.name == args.olt_name)).all()
        )
    if args.all:
        return list(
            db.scalars(
                select(OLTDevice)
                .where(OLTDevice.is_active.is_(True))
                .where(OLTDevice.mgmt_ip.isnot(None))
                .order_by(OLTDevice.name)
            ).all()
        )
    return []


def _slug(value: str | None) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _dump_dir_for_olt(olt: OLTDevice, dump_root: str | None):
    if not dump_root:
        return None
    root = Path(dump_root)
    if not root.exists():
        return None
    olt_tokens = {
        _slug(olt.name),
        _slug(getattr(olt, "hostname", None)),
        _slug(str(getattr(olt, "hostname", "")).replace("-olt", "")),
        _slug(str(olt.name).replace("-olt", "")),
    }
    for child in root.iterdir():
        if not child.is_dir():
            continue
        child_slug = _slug(child.name)
        if child_slug in olt_tokens or any(
            token and (token in child_slug or child_slug in token)
            for token in olt_tokens
        ):
            return child
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import OLT line/service profiles and ONT registrations"
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--olt-id", help="OLT UUID to import")
    target.add_argument("--olt-name", help="OLT name to import")
    target.add_argument("--all", action="store_true", help="Import all active OLTs")
    parser.add_argument(
        "--dump-dir",
        help="Import one OLT from this audit dump directory instead of live SSH",
    )
    parser.add_argument(
        "--dump-root",
        help="Import matching OLTs from child directories under this audit dump root",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        olts = _selected_olts(db, args)
        if not olts:
            logger.error("No matching OLTs found")
            return 1

        failures = 0
        for olt in olts:
            logger.info("Importing OLT state for %s (%s)", olt.name, olt.mgmt_ip)
            dump_dir = (
                Path(args.dump_dir)
                if args.dump_dir
                else _dump_dir_for_olt(olt, args.dump_root)
            )
            if dump_dir:
                logger.info("Using audit dump %s", dump_dir)
                result = import_olt_state_from_dump(db, str(olt.id), dump_dir)
            else:
                result = import_olt_state(db, str(olt.id))
            if result.success:
                db.commit()
            else:
                db.rollback()
                failures += 1
            logger.info(
                "%s: line=%d service=%d onts=%d mappings=%d",
                result.message,
                result.line_profiles,
                result.service_profiles,
                result.ont_registrations,
                result.profile_mappings,
            )
            for warning in result.warnings:
                logger.warning("%s", warning)

        return 1 if failures else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
