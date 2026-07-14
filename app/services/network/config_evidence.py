"""Unified read-only config evidence and drift summaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.network import (
    OLTDevice,
    OltLineProfile,
    OltProfileBundle,
    OltServicePort,
    OntConfigSnapshot,
    OntUnit,
)
from app.services.common import coerce_uuid
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.olt_config_audit import (
    OntConfigCoverage,
    audit_olt_config_backup,
    latest_valid_backup,
)
from app.services.network.olt_operations import olt_backup_base_dir
from app.services.network.ont_config_snapshots import snapshot_integrity_valid
from app.services.network.serial_utils import parse_ont_id_on_olt


@dataclass(frozen=True)
class EvidenceSource:
    label: str
    status: str
    message: str
    observed_at: datetime | None = None
    href: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "status": self.status,
            "message": self.message,
            "observed_at": self.observed_at,
            "href": self.href,
        }


@dataclass(frozen=True)
class DriftCheck:
    label: str
    status: str
    expected: Any
    observed: Any
    source: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "status": self.status,
            "expected": self.expected,
            "observed": self.observed,
            "source": self.source,
            "message": self.message,
        }


def build_ont_config_evidence(db: Session, ont_id: str | UUID) -> dict[str, Any]:
    """Build read-only config evidence for one ONT."""
    ont = db.get(OntUnit, coerce_uuid(ont_id))
    if ont is None:
        return {
            "status": "unknown",
            "summary": "ONT not found",
            "sources": [],
            "drift_checks": [],
            "counts": {"in_sync": 0, "drift": 0, "unknown": 0},
        }

    effective = resolve_effective_ont_config(db, ont)
    effective_values = (
        effective.get("values", {}) if isinstance(effective, dict) else {}
    )
    olt = db.get(OLTDevice, ont.olt_device_id) if ont.olt_device_id else None
    imported_ports = _imported_service_ports(db, ont)
    latest_snapshot = _latest_ont_snapshot(db, ont.id)
    backup_coverage = _backup_coverage(db, olt, ont) if olt is not None else None
    sources = _ont_sources(
        db=db,
        ont=ont,
        olt=olt,
        imported_ports=imported_ports,
        latest_snapshot=latest_snapshot,
        backup_coverage=backup_coverage,
    )
    drift_checks = _ont_drift_checks(
        effective_values=effective_values,
        imported_ports=imported_ports,
        backup_coverage=backup_coverage,
        latest_snapshot=latest_snapshot,
    )
    counts = _status_counts(drift_checks)
    status = _summary_status(counts)
    return {
        "status": status,
        "summary": _summary_message(status, counts),
        "sources": [source.to_dict() for source in sources],
        "drift_checks": [check.to_dict() for check in drift_checks],
        "counts": counts,
    }


def build_olt_config_evidence(db: Session, olt_id: str | UUID) -> dict[str, Any]:
    """Build read-only config evidence for one OLT."""
    olt = db.get(OLTDevice, coerce_uuid(olt_id))
    if olt is None:
        return {
            "status": "unknown",
            "summary": "OLT not found",
            "sources": [],
            "counts": {},
        }

    backup = latest_valid_backup(db, olt.id)
    imported_profile_count = db.scalar(
        select(func.count(OltLineProfile.id)).where(OltLineProfile.olt_id == olt.id)
    )
    imported_service_port_count = db.scalar(
        select(func.count(OltServicePort.id)).where(
            OltServicePort.olt_device_id == olt.id
        )
    )
    bundle_counts = {
        str(status or "unknown"): int(count)
        for status, count in db.execute(
            select(OltProfileBundle.drift_status, func.count(OltProfileBundle.id))
            .where(OltProfileBundle.olt_id == olt.id)
            .where(OltProfileBundle.is_active.is_(True))
            .group_by(OltProfileBundle.drift_status)
        )
    }
    sources = [
        EvidenceSource(
            label="Latest running-config backup",
            status="ok" if backup else "missing",
            message=backup.file_path if backup else "No valid OLT backup found",
            observed_at=backup.created_at if backup else None,
            href=f"/admin/network/olts/backups/{backup.id}" if backup else None,
        ),
        EvidenceSource(
            label="Imported profiles",
            status="ok" if imported_profile_count else "missing",
            message=f"{int(imported_profile_count or 0)} line profile(s)",
        ),
        EvidenceSource(
            label="Imported service ports",
            status="ok" if imported_service_port_count else "missing",
            message=f"{int(imported_service_port_count or 0)} service-port row(s)",
        ),
        EvidenceSource(
            label="Profile bundle drift",
            status="ok" if not bundle_counts.get("drifted") else "drift",
            message=_bundle_counts_message(bundle_counts),
        ),
    ]
    missing = sum(1 for source in sources if source.status == "missing")
    drift = sum(1 for source in sources if source.status == "drift")
    status = "drift" if drift else "unknown" if missing else "in_sync"
    return {
        "status": status,
        "summary": _summary_message(
            status,
            {
                "in_sync": len(sources) - missing - drift,
                "drift": drift,
                "unknown": missing,
            },
        ),
        "sources": [source.to_dict() for source in sources],
        "counts": {
            "imported_profiles": int(imported_profile_count or 0),
            "imported_service_ports": int(imported_service_port_count or 0),
            "bundle_drift": bundle_counts,
        },
    }


def _backup_coverage(
    db: Session, olt: OLTDevice, ont: OntUnit
) -> OntConfigCoverage | None:
    try:
        report = audit_olt_config_backup(
            db,
            olt,
            backup_base_dir=olt_backup_base_dir(),
        )
    except Exception:
        return None
    for coverage in report.ont_coverages:
        if coverage.ont_unit_id == ont.id:
            return coverage
    return None


def _imported_service_ports(db: Session, ont: OntUnit) -> list[OltServicePort]:
    query = select(OltServicePort).where(OltServicePort.ont_unit_id == ont.id)
    if ont.olt_device_id:
        fsp = _ont_fsp(ont)
        ont_id_on_olt = parse_ont_id_on_olt(ont.external_id)
        if fsp and ont_id_on_olt is not None:
            query = select(OltServicePort).where(
                OltServicePort.olt_device_id == ont.olt_device_id,
                or_(
                    OltServicePort.ont_unit_id == ont.id,
                    and_(
                        OltServicePort.fsp == fsp,
                        OltServicePort.ont_id_on_olt == ont_id_on_olt,
                    ),
                ),
            )
    return list(db.scalars(query.order_by(OltServicePort.port_index.asc())).all())


def _latest_ont_snapshot(db: Session, ont_id: UUID) -> OntConfigSnapshot | None:
    return db.scalars(
        select(OntConfigSnapshot)
        .where(OntConfigSnapshot.ont_unit_id == ont_id)
        .order_by(OntConfigSnapshot.created_at.desc())
        .limit(1)
    ).first()


def _ont_sources(
    *,
    db: Session,
    ont: OntUnit,
    olt: OLTDevice | None,
    imported_ports: list[OltServicePort],
    latest_snapshot: OntConfigSnapshot | None,
    backup_coverage: OntConfigCoverage | None,
) -> list[EvidenceSource]:
    latest_backup = latest_valid_backup(db, olt.id) if olt is not None else None
    backup_href = (
        f"/admin/network/olts/backups/{latest_backup.id}" if latest_backup else None
    )
    backup_status = "missing"
    backup_message = "No backup evidence for this ONT"
    if backup_coverage is not None:
        if "missing_valid_olt_backup" in backup_coverage.gaps:
            backup_message = "No valid OLT running-config backup"
        elif backup_coverage.found_registration:
            backup_status = "ok"
            backup_message = (
                f"Registered at {backup_coverage.registration.fsp} "
                f"ONT {backup_coverage.registration.ont_id}"
                if backup_coverage.registration
                else "Registered in backup"
            )
        else:
            backup_status = "drift"
            backup_message = "ONT registration missing from latest backup"
    snapshot_integrity = (
        snapshot_integrity_valid(latest_snapshot) if latest_snapshot else None
    )
    snapshot_status = (
        "drift"
        if snapshot_integrity is False
        else "ok"
        if latest_snapshot
        else "missing"
    )
    snapshot_message = "No saved ONT config snapshot"
    if latest_snapshot is not None:
        integrity_label = (
            "integrity verified"
            if snapshot_integrity is True
            else "integrity failed"
            if snapshot_integrity is False
            else "legacy, no checksum"
        )
        snapshot_message = f"Snapshot {latest_snapshot.created_at} ({integrity_label})"
    return [
        EvidenceSource(
            label="Effective intent",
            status="ok",
            message="Resolved desired/effective ONT config",
            href=f"/admin/network/onts/{ont.id}?tab=configuration",
        ),
        EvidenceSource(
            label="Latest OLT backup",
            status=backup_status,
            message=backup_message,
            href=backup_href,
        ),
        EvidenceSource(
            label="Imported service-port state",
            status="ok" if imported_ports else "missing",
            message=f"{len(imported_ports)} imported service-port row(s)",
        ),
        EvidenceSource(
            label="ONT config snapshot",
            status=snapshot_status,
            message=snapshot_message,
            observed_at=latest_snapshot.created_at if latest_snapshot else None,
        ),
    ]


def _ont_drift_checks(
    *,
    effective_values: dict[str, Any],
    imported_ports: list[OltServicePort],
    backup_coverage: OntConfigCoverage | None,
    latest_snapshot: OntConfigSnapshot | None,
) -> list[DriftCheck]:
    snapshot_integrity = (
        snapshot_integrity_valid(latest_snapshot) if latest_snapshot else None
    )
    trusted_snapshot = latest_snapshot if snapshot_integrity is not False else None
    checks = [
        _compare_membership(
            label="WAN VLAN",
            expected=effective_values.get("wan_vlan"),
            observed=[port.vlan_id for port in imported_ports],
            source="Imported service ports",
        ),
        _compare_membership(
            label="WAN GEM",
            expected=effective_values.get("wan_gem_index"),
            observed=[port.gem_index for port in imported_ports],
            source="Imported service ports",
        ),
    ]
    registration = backup_coverage.registration if backup_coverage else None
    checks.extend(
        [
            _compare_scalar(
                label="Line profile",
                expected=effective_values.get("authorization_line_profile_id"),
                observed=registration.line_profile_id if registration else None,
                source="Latest OLT backup",
            ),
            _compare_scalar(
                label="Service profile",
                expected=effective_values.get("authorization_service_profile_id"),
                observed=registration.service_profile_id if registration else None,
                source="Latest OLT backup",
            ),
            _compare_snapshot_value(
                label="WiFi SSID",
                expected=effective_values.get("wifi_ssid"),
                snapshot=trusted_snapshot,
                section="wifi",
                keys=("SSID", "WiFi SSID", "SSID 1", "2.4GHz SSID"),
            ),
            _compare_snapshot_value(
                label="WAN mode",
                expected=effective_values.get("wan_mode"),
                snapshot=trusted_snapshot,
                section="wan",
                keys=("Connection Type", "WAN Mode", "Mode"),
            ),
        ]
    )
    if snapshot_integrity is False:
        checks.append(
            DriftCheck(
                label="Snapshot integrity",
                status="drift",
                expected="valid checksum",
                observed="checksum mismatch",
                source="ONT config snapshot",
                message="Snapshot payload changed after capture",
            )
        )
    return checks


def _compare_membership(
    *,
    label: str,
    expected: Any,
    observed: list[Any],
    source: str,
) -> DriftCheck:
    normalized_expected = _normalize_value(expected)
    normalized_observed = [_normalize_value(item) for item in observed]
    if normalized_expected is None:
        return DriftCheck(
            label, "unknown", expected, observed, source, "No intent value"
        )
    if not normalized_observed:
        return DriftCheck(
            label, "unknown", expected, observed, source, "No observed value"
        )
    if normalized_expected in normalized_observed:
        return DriftCheck(label, "in_sync", expected, observed, source, "Matches")
    return DriftCheck(
        label, "drift", expected, observed, source, "Observed value differs"
    )


def _compare_scalar(
    *,
    label: str,
    expected: Any,
    observed: Any,
    source: str,
) -> DriftCheck:
    normalized_expected = _normalize_value(expected)
    normalized_observed = _normalize_value(observed)
    if normalized_expected is None:
        return DriftCheck(
            label, "unknown", expected, observed, source, "No intent value"
        )
    if normalized_observed is None:
        return DriftCheck(
            label, "unknown", expected, observed, source, "No observed value"
        )
    if normalized_expected == normalized_observed:
        return DriftCheck(label, "in_sync", expected, observed, source, "Matches")
    return DriftCheck(
        label, "drift", expected, observed, source, "Observed value differs"
    )


def _compare_snapshot_value(
    *,
    label: str,
    expected: Any,
    snapshot: OntConfigSnapshot | None,
    section: str,
    keys: tuple[str, ...],
) -> DriftCheck:
    observed = None
    if snapshot is not None:
        payload = getattr(snapshot, section, None)
        if isinstance(payload, dict):
            for key in keys:
                if key in payload:
                    observed = payload[key]
                    break
    return _compare_scalar(
        label=label,
        expected=expected,
        observed=observed,
        source="ONT config snapshot",
    )


def _status_counts(checks: list[DriftCheck]) -> dict[str, int]:
    counts = {"in_sync": 0, "drift": 0, "unknown": 0}
    for check in checks:
        if check.status == "drift":
            counts["drift"] += 1
        elif check.status == "in_sync":
            counts["in_sync"] += 1
        else:
            counts["unknown"] += 1
    return counts


def _summary_status(counts: dict[str, int]) -> str:
    if counts.get("drift", 0):
        return "drift"
    if counts.get("unknown", 0):
        return "unknown"
    return "in_sync"


def _summary_message(status: str, counts: dict[str, int]) -> str:
    if status == "drift":
        return f"{counts.get('drift', 0)} drift check(s) need attention"
    if status == "unknown":
        return f"{counts.get('unknown', 0)} drift check(s) need more evidence"
    return f"{counts.get('in_sync', 0)} drift check(s) in sync"


def _bundle_counts_message(counts: dict[str, int]) -> str:
    if not counts:
        return "No active profile bundles"
    return ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))


def _normalize_value(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip().lower()


def _ont_fsp(ont: OntUnit) -> str | None:
    if ont.board and ont.port is not None:
        return f"{ont.board}/{ont.port}"
    return None
