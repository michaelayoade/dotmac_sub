"""Layer 3 reconciliation inputs/outputs: CoA outcomes, enforcement paths, canary.

``compute_service_path`` is pure (counts in, findings + scopes out).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID

from app.services.router_health import findings as findings_service
from app.services.router_health import service_checks
from app.services.router_health.contracts import (
    CheckId,
    HealthFinding,
    HealthLayer,
    PostureFinding,
    SubjectKind,
)
from app.services.router_health.service_checks import EnforcementFailureRow
from app.services.router_health.settings import RouterHealthSettings

SERVICE_CHECKS = (
    CheckId.coa_timeouts,
    CheckId.enforcement_path_failing,
    CheckId.coa_canary_stale,
)


@dataclass(frozen=True)
class NasInput:
    nas_device_id: UUID
    name: str
    router_id: UUID | None
    router_name: str | None
    canary_eligible: bool
    #: ``last_at`` of the newest canary bucket for this NAS, if any.
    last_canary_at: datetime | None
    #: disposition -> count over the CoA window (disconnect + update kinds).
    coa_counts: Mapping[str, int]
    enforcement_failures: Sequence[EnforcementFailureRow]


@dataclass(frozen=True)
class ServicePathComputation:
    findings: tuple[HealthFinding, ...]
    scopes: tuple[findings_service.Scope, ...]
    retire_subjects: tuple[tuple[HealthLayer, UUID], ...]
    nas_count: int


def nas_label(name: str, router_name: str | None) -> str:
    return f"{name} ({router_name})" if router_name else name


def nas_url(nas: NasInput) -> str:
    if nas.router_id is not None:
        return f"/admin/network/routers/{nas.router_id}"
    return f"/admin/network/nas/devices/{nas.nas_device_id}"


def to_nas_finding(pf: PostureFinding, nas: NasInput) -> HealthFinding:
    return HealthFinding(
        layer=HealthLayer.service,
        subject_kind=SubjectKind.nas,
        subject_id=nas.nas_device_id,
        check=pf.check,
        qualifier=pf.qualifier,
        priority=pf.priority,
        title=pf.title,
        impact=pf.impact,
        evidence=pf.evidence,
        fix=pf.fix,
        target_url=nas_url(nas),
        router_id=nas.router_id,
        nas_device_id=nas.nas_device_id,
        known_issue=pf.known_issue,
    )


def compute_service_path(
    *,
    nas_rows: Sequence[NasInput],
    settings: RouterHealthSettings,
    canary_enabled: bool,
    now: datetime,
    annotations: Mapping[UUID, Mapping[str, str]] | None = None,
    open_subjects: set[UUID] | None = None,
) -> ServicePathComputation:
    findings: list[HealthFinding] = []
    scopes: list[findings_service.Scope] = []
    notes = annotations or {}
    stale_after = timedelta(hours=2 * settings.coa_canary_interval_hours)
    for nas in nas_rows:
        label = nas_label(nas.name, nas.router_name)
        produced: list[PostureFinding] = []
        produced += service_checks.evaluate_coa_timeouts(
            label,
            nas.coa_counts,
            window_hours=settings.coa_window_hours,
            min_count=settings.coa_timeout_min_count,
            ratio_pct=settings.coa_timeout_ratio_pct,
        )
        produced += service_checks.evaluate_enforcement_paths(
            label,
            nas.enforcement_failures,
            min_count=settings.enforcement_failure_min_count,
        )
        if canary_enabled and nas.canary_eligible:
            if nas.last_canary_at is None or now - nas.last_canary_at > stale_after:
                produced.append(
                    service_checks.evaluate_canary_stale(
                        label, interval_hours=settings.coa_canary_interval_hours
                    )
                )
        nas_notes = notes.get(nas.nas_device_id, {})
        for pf in produced:
            finding = to_nas_finding(pf, nas)
            issue = nas_notes.get(pf.check.value)
            if issue:
                finding = replace(finding, known_issue=issue)
            findings.append(finding)
        for check in SERVICE_CHECKS:
            if check is CheckId.coa_canary_stale and not canary_enabled:
                continue
            scopes.append(
                findings_service.Scope(HealthLayer.service, nas.nas_device_id, check)
            )
    active = {n.nas_device_id for n in nas_rows}
    retire = tuple(
        (HealthLayer.service, subject)
        for subject in sorted((open_subjects or set()) - active, key=str)
    )
    return ServicePathComputation(
        findings=tuple(findings),
        scopes=tuple(scopes),
        retire_subjects=retire,
        nas_count=len(nas_rows),
    )
