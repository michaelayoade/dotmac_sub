#!/usr/bin/env python3
"""Audit and clean synthetic SNMP-created ONT inventory rows.

Default mode is dry-run. Use --apply to promote safe real vendor serials and
deactivate safe SNMP-only rows. Risky rows are never modified; they are written
to a CSV review report.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.network import OntAssignment, OntUnit
from app.models.tr069 import Tr069CpeDevice
from app.services.network.ont_serials import (
    is_plausible_vendor_serial,
    looks_synthetic_ont_serial,
    normalize_ont_serial,
)


@dataclass
class AuditRow:
    ont: OntUnit
    bucket: str
    reason: str
    active_assignments: int
    subscriber_assignments: int
    tr069_links: int
    has_pppoe: bool
    has_manual_context: bool
    real_serial_conflict: bool


def _active_assignment_counts(db: Session, ont_id: Any) -> tuple[int, int]:
    rows = db.execute(
        select(
            func.count(OntAssignment.id),
            func.count(OntAssignment.subscriber_id),
        ).where(
            OntAssignment.ont_unit_id == ont_id,
            OntAssignment.active.is_(True),
        )
    ).one()
    return int(rows[0] or 0), int(rows[1] or 0)


def _tr069_link_count(db: Session, ont_id: Any) -> int:
    return int(
        db.scalar(
            select(func.count(Tr069CpeDevice.id)).where(
                Tr069CpeDevice.ont_unit_id == ont_id,
                Tr069CpeDevice.is_active.is_(True),
            )
        )
        or 0
    )


def _has_manual_context(ont: OntUnit) -> bool:
    fields = (
        "name",
        "address_or_comment",
        "contact",
        "notes",
        "mgmt_ip_address",
    )
    return any(str(getattr(ont, field, "") or "").strip() for field in fields)


def _real_serial_conflict(db: Session, ont: OntUnit, real_serial: str | None) -> bool:
    normalized = normalize_ont_serial(real_serial)
    if not normalized:
        return False
    rows = db.scalars(select(OntUnit).where(OntUnit.id != ont.id)).all()
    return any(
        normalize_ont_serial(getattr(row, "serial_number", None)) == normalized
        for row in rows
    )


def _audit_ont(db: Session, ont: OntUnit) -> AuditRow:
    active_assignments, subscriber_assignments = _active_assignment_counts(db, ont.id)
    tr069_links = _tr069_link_count(db, ont.id)
    has_pppoe = bool(str(getattr(ont, "pppoe_username", "") or "").strip())
    has_manual_context = _has_manual_context(ont)
    vendor_serial = str(getattr(ont, "vendor_serial_number", "") or "").strip()
    has_real_vendor_serial = (
        bool(vendor_serial)
        and not looks_synthetic_ont_serial(vendor_serial)
        and is_plausible_vendor_serial(vendor_serial)
    )
    conflict = _real_serial_conflict(db, ont, vendor_serial if has_real_vendor_serial else None)

    if has_real_vendor_serial and not conflict:
        return AuditRow(
            ont=ont,
            bucket="promote_real_serial",
            reason="synthetic serial has plausible vendor_serial_number",
            active_assignments=active_assignments,
            subscriber_assignments=subscriber_assignments,
            tr069_links=tr069_links,
            has_pppoe=has_pppoe,
            has_manual_context=has_manual_context,
            real_serial_conflict=conflict,
        )

    risky_reasons: list[str] = []
    if conflict:
        risky_reasons.append("vendor_serial_number conflicts with another ONT")
    if subscriber_assignments:
        risky_reasons.append("has subscriber assignment")
    if tr069_links:
        risky_reasons.append("has active TR-069 link")
    if has_pppoe:
        risky_reasons.append("has PPPoE username")
    if has_manual_context:
        risky_reasons.append("has manual context fields")

    if risky_reasons:
        return AuditRow(
            ont=ont,
            bucket="manual_review",
            reason="; ".join(risky_reasons),
            active_assignments=active_assignments,
            subscriber_assignments=subscriber_assignments,
            tr069_links=tr069_links,
            has_pppoe=has_pppoe,
            has_manual_context=has_manual_context,
            real_serial_conflict=conflict,
        )

    return AuditRow(
        ont=ont,
        bucket="safe_deactivate",
        reason="synthetic SNMP-only ONT has no real serial or operational links",
        active_assignments=active_assignments,
        subscriber_assignments=subscriber_assignments,
        tr069_links=tr069_links,
        has_pppoe=has_pppoe,
        has_manual_context=has_manual_context,
        real_serial_conflict=conflict,
    )


def _write_report(path: Path, rows: list[AuditRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "bucket",
                "reason",
                "ont_id",
                "serial_number",
                "vendor_serial_number",
                "olt_device_id",
                "external_id",
                "board",
                "port",
                "is_active",
                "active_assignments",
                "subscriber_assignments",
                "tr069_links",
                "has_pppoe",
                "has_manual_context",
                "real_serial_conflict",
                "last_seen_at",
                "last_sync_source",
            ],
        )
        writer.writeheader()
        for row in rows:
            ont = row.ont
            writer.writerow(
                {
                    "bucket": row.bucket,
                    "reason": row.reason,
                    "ont_id": str(ont.id),
                    "serial_number": ont.serial_number,
                    "vendor_serial_number": ont.vendor_serial_number,
                    "olt_device_id": str(ont.olt_device_id) if ont.olt_device_id else "",
                    "external_id": ont.external_id,
                    "board": ont.board,
                    "port": ont.port,
                    "is_active": ont.is_active,
                    "active_assignments": row.active_assignments,
                    "subscriber_assignments": row.subscriber_assignments,
                    "tr069_links": row.tr069_links,
                    "has_pppoe": row.has_pppoe,
                    "has_manual_context": row.has_manual_context,
                    "real_serial_conflict": row.real_serial_conflict,
                    "last_seen_at": ont.last_seen_at,
                    "last_sync_source": ont.last_sync_source,
                }
            )


def _apply_cleanup(rows: list[AuditRow]) -> tuple[int, int]:
    promoted = 0
    deactivated = 0
    for row in rows:
        ont = row.ont
        if row.bucket == "promote_real_serial":
            ont.serial_number = str(ont.vendor_serial_number or "").strip()
            promoted += 1
        elif row.bucket == "safe_deactivate":
            ont.is_active = False
            deactivated += 1
    return promoted, deactivated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply safe promotions/deactivations. Default is dry-run.",
    )
    parser.add_argument(
        "--report",
        default="synthetic_ont_cleanup_report.csv",
        help="CSV report path.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        onts = list(db.scalars(select(OntUnit).order_by(OntUnit.created_at)).all())
        rows = [
            _audit_ont(db, ont)
            for ont in onts
            if looks_synthetic_ont_serial(getattr(ont, "serial_number", None))
        ]
        _write_report(Path(args.report), rows)

        counts = {
            "promote_real_serial": 0,
            "safe_deactivate": 0,
            "manual_review": 0,
        }
        for row in rows:
            counts[row.bucket] = counts.get(row.bucket, 0) + 1

        print("Synthetic ONT cleanup audit")
        print(f"  total synthetic: {len(rows)}")
        print(f"  promote real serial: {counts.get('promote_real_serial', 0)}")
        print(f"  safe deactivate: {counts.get('safe_deactivate', 0)}")
        print(f"  manual review: {counts.get('manual_review', 0)}")
        print(f"  report: {args.report}")

        if args.apply:
            promoted, deactivated = _apply_cleanup(rows)
            db.commit()
            print(f"Applied cleanup: promoted={promoted}, deactivated={deactivated}")
        else:
            db.rollback()
            print("Dry-run only. Re-run with --apply to modify safe rows.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
