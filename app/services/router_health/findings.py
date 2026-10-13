"""Fingerprints, severity mapping and scoped sync/resolve of router health findings.

Findings reach the alert console only through ``observability.record_finding`` /
``resolve_findings``; this module never writes ``AdminAlert`` itself. In
``shadow`` mode findings are visible but queue no notifications.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.network_monitoring import AlertSeverity
from app.services import observability
from app.services.router_health.contracts import (
    ALERT_MODE_ALERT,
    DETAILS_SCHEMA,
    FINGERPRINT_ROOT,
    CheckId,
    HealthFinding,
    HealthLayer,
    HealthPriority,
    SubjectKind,
)

ALERT_CATEGORY = "network"
ALERT_SOURCE = "router_health"
TITLE_MAX = 120
SUMMARY_MAX = 255
_QUALIFIER_RE = re.compile(r"^[a-z0-9.-]{1,40}$")
_PRIORITY_RANK = {
    HealthPriority.critical: 0,
    HealthPriority.high: 1,
    HealthPriority.medium: 2,
    HealthPriority.low: 3,
}

#: critical -> critical; high is mapped DOWN to warning (``details.priority``
#: keeps "high" for ordering); low -> info.
SEVERITY_BY_PRIORITY: dict[HealthPriority, AlertSeverity] = {
    HealthPriority.critical: AlertSeverity.critical,
    HealthPriority.high: AlertSeverity.warning,
    HealthPriority.medium: AlertSeverity.warning,
    HealthPriority.low: AlertSeverity.info,
}


def normalize_qualifier(qualifier: str) -> str:
    if qualifier == "0" or _QUALIFIER_RE.fullmatch(qualifier):
        return qualifier
    return hashlib.sha1(qualifier.encode("utf-8"), usedforsecurity=False).hexdigest()[
        :12
    ]


def subject_prefix(layer: HealthLayer, subject_id: UUID) -> str:
    return f"{FINGERPRINT_ROOT}{layer.value}:{subject_id}:"


def check_prefix(layer: HealthLayer, subject_id: UUID, check: CheckId) -> str:
    return f"{subject_prefix(layer, subject_id)}{check.value}:"


def fingerprint(finding: HealthFinding) -> str:
    return (
        f"{check_prefix(finding.layer, finding.subject_id, finding.check)}"
        f"{normalize_qualifier(finding.qualifier)}"
    )


def to_observability_finding(
    finding: HealthFinding, *, mode: str
) -> observability.Finding:
    details: dict[str, object] = {
        "schema": DETAILS_SCHEMA,
        "router_id": str(finding.router_id) if finding.router_id else None,
        "nas_device_id": str(finding.nas_device_id) if finding.nas_device_id else None,
        "layer": finding.layer.value,
        "check": finding.check.value,
        "qualifier": finding.qualifier,
        "priority": finding.priority.value,
        "impact": finding.impact,
        "evidence": dict(finding.evidence),
        "fix": finding.fix,
        "known_issue": finding.known_issue,
        "policy_ref": finding.policy_note,
        "mode": mode,
        "snapshot_id": str(finding.snapshot_id) if finding.snapshot_id else None,
        "snapshot_at": (
            finding.snapshot_at.isoformat() if finding.snapshot_at else None
        ),
    }
    return observability.Finding(
        fingerprint=fingerprint(finding),
        domain=ALERT_CATEGORY,
        source=ALERT_SOURCE,
        severity=SEVERITY_BY_PRIORITY[finding.priority],
        title=finding.title[:TITLE_MAX],
        summary=finding.impact[:SUMMARY_MAX],
        details=details,
        target_url=finding.target_url,
    )


@dataclass(frozen=True)
class Scope:
    """A (layer, subject, check) whose findings are fully re-judged this run."""

    layer: HealthLayer
    subject_id: UUID
    check: CheckId


@dataclass(frozen=True)
class SyncOutcome:
    opened: int = 0
    updated: int = 0
    escalated: int = 0
    resolved: int = 0
    capped: int = 0


def order_by_priority(findings: Iterable[HealthFinding]) -> list[HealthFinding]:
    return sorted(
        findings,
        key=lambda f: (_PRIORITY_RANK[f.priority], fingerprint(f)),
    )


def cap_findings(
    findings: Sequence[HealthFinding], limit: int
) -> tuple[list[HealthFinding], int]:
    """Keep the ``limit`` most urgent findings; return (kept, dropped_count)."""
    ordered = order_by_priority(findings)
    if len(ordered) <= limit:
        return ordered, 0
    return ordered[:limit], len(ordered) - limit


def sync_findings(
    db: Session,
    *,
    findings: Sequence[HealthFinding],
    scopes: Iterable[Scope],
    mode: str,
) -> SyncOutcome:
    """Record findings and resolve cleared ones within the judged scopes only.

    Flush-only: the caller (an owner command) owns the commit. Alerts outside
    ``network:router-health:`` and findings outside ``scopes`` are never touched.
    """
    notify = mode == ALERT_MODE_ALERT
    counts: dict[str, int] = defaultdict(int)
    active_by_scope: dict[str, set[str]] = defaultdict(set)
    for finding in findings:
        recorded_fp = fingerprint(finding)
        outcome = observability.record_finding(
            db, to_observability_finding(finding, mode=mode), notify=notify
        )
        counts[outcome] += 1
        prefix = check_prefix(finding.layer, finding.subject_id, finding.check)
        active_by_scope[prefix].add(recorded_fp)
    resolved = 0
    for scope in scopes:
        prefix = check_prefix(scope.layer, scope.subject_id, scope.check)
        resolved += observability.resolve_findings(
            db,
            managed_prefix=prefix,
            active_fingerprints=active_by_scope.get(prefix, set()),
        )
    return SyncOutcome(
        opened=counts["opened"],
        updated=counts["updated"],
        escalated=counts["escalated"],
        resolved=resolved,
    )


def resolve_subjects(
    db: Session, *, layer: HealthLayer, subject_ids: Iterable[UUID]
) -> int:
    """Resolve every finding of a layer for subjects that are no longer active."""
    resolved = 0
    for subject_id in subject_ids:
        resolved += observability.resolve_findings(
            db,
            managed_prefix=subject_prefix(layer, subject_id),
            active_fingerprints=set(),
        )
    return resolved


def resolve_all(db: Session) -> int:
    """Rollback path: resolve every router-health alert."""
    return observability.resolve_findings(
        db, managed_prefix=FINGERPRINT_ROOT, active_fingerprints=set()
    )


def subject_target_url(
    subject_kind: SubjectKind, *, router_id: UUID | None, nas_device_id: UUID | None
) -> str:
    if router_id is not None:
        return f"/admin/network/routers/{router_id}#health"
    if nas_device_id is not None:
        return f"/admin/network/nas/devices/{nas_device_id}"
    return "/admin/alerts?category=network"
