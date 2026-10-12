"""Snapshot staleness and export-failure evaluation (layer 1, from DB rows).

Reads no router: the inputs are the latest ``RouterConfigSnapshot`` time and the
``config_export`` signal row written by the snapshot task's outcome command.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.services.router_health.contracts import (
    CheckId,
    ConfigExportFailureClass,
    EvidenceValue,
    HealthLayer,
    HealthPriority,
    PostureFinding,
)

_FIX_BY_CLASS: dict[str, str] = {
    ConfigExportFailureClass.auth_failed.value: (
        "Create the dotmac-ops user (group=read, key dotmac-ops@automation) "
        "with address including the sub-prod source."
    ),
    ConfigExportFailureClass.unreachable.value: (
        "Check the management path and service ACL for the sub-prod source on "
        "the SSH port."
    ),
    ConfigExportFailureClass.host_key_changed.value: (
        "Verify the device (replacement?) before re-pinning the key in the "
        "known_hosts file."
    ),
    ConfigExportFailureClass.host_key_unknown.value: (
        "Pre-seed this router's SSH host key in the known_hosts file from a "
        "trusted path; Sub does not trust a host key on first use."
    ),
    ConfigExportFailureClass.empty_export.value: (
        "The router returned an empty export; check the dotmac-ops user policy."
    ),
    ConfigExportFailureClass.no_auth_configured.value: (
        "Configure the snapshot SSH key or password on the Sub workers."
    ),
    ConfigExportFailureClass.other.value: (
        "Check the router and the Sub worker logs for the export failure."
    ),
}
_DEFAULT_FIX = (
    "Check that Sub can reach the router over SSH and export its configuration."
)


@dataclass(frozen=True)
class SnapshotView:
    """Read model for one active router, built by the reconciler."""

    router_label: str
    latest_snapshot_at: datetime | None
    export_status: str | None = None
    export_failure_class: str | None = None
    export_consecutive_failures: int = 0
    export_last_attempt_at: datetime | None = None


def humanize_age(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} d"


def fix_for_class(failure_class: str | None) -> str:
    if failure_class is None:
        return _DEFAULT_FIX
    return _FIX_BY_CLASS.get(failure_class, _DEFAULT_FIX)


def evaluate_snapshot(
    view: SnapshotView,
    *,
    now: datetime,
    max_age_hours: int,
    high_age_hours: int,
) -> list[PostureFinding]:
    """Findings for never-captured / stale snapshots and host-key changes."""
    findings: list[PostureFinding] = []
    failing = view.export_status == "abnormal"
    failure_class = view.export_failure_class if failing else None
    evidence: dict[str, EvidenceValue] = {
        "last_snapshot_at": (
            view.latest_snapshot_at.isoformat() if view.latest_snapshot_at else None
        ),
        "last_attempt_at": (
            view.export_last_attempt_at.isoformat()
            if view.export_last_attempt_at
            else None
        ),
        "failure_class": failure_class,
        "consecutive_failures": view.export_consecutive_failures,
    }
    label = view.router_label
    if view.latest_snapshot_at is None:
        findings.append(
            PostureFinding(
                check=CheckId.snapshot_never,
                qualifier="0",
                priority=HealthPriority.high,
                title=f"{label}: Sub has never captured this router's configuration",
                impact="Posture, change history and rollback are blind for this router.",
                evidence=evidence,
                fix=fix_for_class(failure_class),
                layer=HealthLayer.snapshot,
            )
        )
    else:
        age = now - view.latest_snapshot_at
        if age > timedelta(hours=max_age_hours):
            high = age > timedelta(hours=high_age_hours)
            findings.append(
                PostureFinding(
                    check=CheckId.snapshot_stale,
                    qualifier="0",
                    priority=HealthPriority.high if high else HealthPriority.medium,
                    title=f"{label}: configuration snapshot is {humanize_age(age)} old",
                    impact=(
                        "Config drift and suspension bypass on this router would "
                        "go unnoticed."
                    ),
                    evidence={
                        **evidence,
                        "age_hours": int(age.total_seconds() // 3600),
                    },
                    fix=fix_for_class(failure_class),
                    layer=HealthLayer.snapshot,
                )
            )
    if failure_class == ConfigExportFailureClass.host_key_changed.value:
        findings.append(
            PostureFinding(
                check=CheckId.export_host_key_changed,
                qualifier="0",
                priority=HealthPriority.high,
                title=f"{label}: router SSH host key changed",
                impact="Snapshots stopped; possible router replacement or interception.",
                evidence=evidence,
                fix=fix_for_class(failure_class),
                layer=HealthLayer.snapshot,
            )
        )
    return findings


def unparseable_finding(
    label: str, *, coverage: float, identity_found: bool
) -> PostureFinding:
    return PostureFinding(
        check=CheckId.snapshot_unparseable,
        qualifier="0",
        priority=HealthPriority.low,
        title=f"{label}: latest snapshot could not be parsed",
        impact="Posture checks for this router are not evaluated; prior results are kept.",
        evidence={
            "parse_coverage": round(coverage, 4),
            "identity_found": identity_found,
        },
        fix="Inspect the snapshot; the parser may not understand this RouterOS export.",
        layer=HealthLayer.snapshot,
    )
