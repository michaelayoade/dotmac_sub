"""Layer 1 reconciliation: snapshot staleness + config posture (pure computation).

``compute_posture`` takes plain values read from the database and returns the
findings and the scopes they replace. Nothing here touches a router or the
database, so it is exercised with fixtures.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from app.services.router_health import findings as findings_service
from app.services.router_health.contracts import (
    CheckId,
    HealthFinding,
    HealthLayer,
    HealthPriority,
    PostureFinding,
    SubjectKind,
)
from app.services.router_health.policy import (
    EXPIRED_NOTE_DAYS,
    EffectiveRouterPolicy,
    LoadedPolicy,
    effective_policy,
    matching_entries,
    unmatched_entries,
)
from app.services.router_health.posture_checks import evaluate_posture
from app.services.router_health.routeros_export import parse_export
from app.services.router_health.snapshot_checks import (
    SnapshotView,
    evaluate_snapshot,
    unparseable_finding,
)

#: Fixed subject for policy-layer findings (the policy is not a router).
POLICY_SUBJECT_ID = uuid5(NAMESPACE_URL, "dotmac:router-health:policy")
POLICY_TARGET_URL = "/admin/alerts?category=network"

SNAPSHOT_CHECKS = (
    CheckId.snapshot_never,
    CheckId.snapshot_stale,
    CheckId.export_host_key_changed,
    CheckId.snapshot_unparseable,
)
POLICY_CHECKS = (
    CheckId.policy_invalid,
    CheckId.policy_unmatched,
    CheckId.policy_exception_expired,
    CheckId.policy_overflow,
)


@dataclass(frozen=True)
class SnapshotInput:
    snapshot_id: UUID
    created_at: datetime
    config_export: str


@dataclass(frozen=True)
class ExportSignalInput:
    status: str
    failure_class: str | None
    consecutive_failures: int
    last_attempt_at: datetime | None


@dataclass(frozen=True)
class RouterInput:
    router_id: UUID
    name: str
    management_ip: str
    snapshot: SnapshotInput | None
    export_signal: ExportSignalInput | None = None
    linked_to_active_nas: bool = False


@dataclass(frozen=True)
class PostureThresholds:
    snapshot_max_age_hours: int = 36
    snapshot_high_age_hours: int = 168
    max_findings_per_run: int = 300


@dataclass(frozen=True)
class PostureComputation:
    findings: tuple[HealthFinding, ...]
    scopes: tuple[findings_service.Scope, ...]
    retire_subjects: tuple[tuple[HealthLayer, UUID], ...]
    capped: int
    routers: int
    parsed: int
    unparsed: int
    no_snapshot: int


def _router_url(router_id: UUID) -> str:
    return f"/admin/network/routers/{router_id}"


def _hash(*parts: str) -> str:
    return hashlib.sha1(
        "|".join(parts).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:12]


def _health(
    pf: PostureFinding,
    *,
    router: RouterInput,
    snapshot: SnapshotInput | None,
) -> HealthFinding:
    return HealthFinding(
        layer=pf.layer,
        subject_kind=SubjectKind.router,
        subject_id=router.router_id,
        check=pf.check,
        qualifier=pf.qualifier,
        priority=pf.priority,
        title=pf.title,
        impact=pf.impact,
        evidence=pf.evidence,
        fix=pf.fix,
        target_url=_router_url(router.router_id),
        router_id=router.router_id,
        known_issue=pf.known_issue,
        policy_note=pf.policy_note,
        snapshot_id=snapshot.snapshot_id if snapshot else None,
        snapshot_at=snapshot.created_at if snapshot else None,
    )


def _policy_finding(
    check: CheckId,
    *,
    qualifier: str,
    title: str,
    impact: str,
    evidence: Mapping[str, object],
    fix: str,
    router_id: UUID | None = None,
) -> HealthFinding:
    return HealthFinding(
        layer=HealthLayer.policy,
        subject_kind=SubjectKind.policy,
        subject_id=POLICY_SUBJECT_ID,
        check=check,
        qualifier=qualifier,
        priority=HealthPriority.low,
        title=title,
        impact=impact,
        evidence=evidence,  # type: ignore[arg-type]
        fix=fix,
        target_url=_router_url(router_id) if router_id else POLICY_TARGET_URL,
        router_id=router_id,
    )


def compute_posture(
    *,
    routers: Sequence[RouterInput],
    loaded_policy: LoadedPolicy,
    suspended_list: str,
    thresholds: PostureThresholds,
    now: datetime,
    today: date,
    open_subjects: Mapping[HealthLayer, set[UUID]] | None = None,
) -> PostureComputation:
    """Judge every active router; return findings and the scopes they replace."""
    policy = loaded_policy.policy
    findings: list[HealthFinding] = []
    scopes: list[findings_service.Scope] = []
    identities: list[tuple[str | None, str, UUID]] = []
    parsed = unparsed = no_snapshot = 0

    for router in routers:
        snapshot = router.snapshot
        signal = router.export_signal
        view = SnapshotView(
            router_label=router.name,
            latest_snapshot_at=snapshot.created_at if snapshot else None,
            export_status=signal.status if signal else None,
            export_failure_class=signal.failure_class if signal else None,
            export_consecutive_failures=signal.consecutive_failures if signal else 0,
            export_last_attempt_at=signal.last_attempt_at if signal else None,
        )
        for pf in evaluate_snapshot(
            view,
            now=now,
            max_age_hours=thresholds.snapshot_max_age_hours,
            high_age_hours=thresholds.snapshot_high_age_hours,
        ):
            findings.append(_health(pf, router=router, snapshot=snapshot))
        for check in SNAPSHOT_CHECKS:
            scopes.append(
                findings_service.Scope(HealthLayer.snapshot, router.router_id, check)
            )
        if snapshot is None:
            no_snapshot += 1
            identities.append((None, router.name, router.router_id))
            continue
        doc = parse_export(snapshot.config_export)
        identities.append((doc.identity, router.name, router.router_id))
        if not doc.parsed:
            unparsed += 1
            findings.append(
                _health(
                    unparseable_finding(
                        router.name,
                        coverage=doc.coverage,
                        identity_found=doc.identity is not None,
                    ),
                    router=router,
                    snapshot=snapshot,
                )
            )
            # Posture findings are carried forward: no posture scope is judged.
            continue
        parsed += 1
        eff: EffectiveRouterPolicy = effective_policy(
            policy,
            identity=doc.identity,
            router_name=router.name,
            router_id=router.router_id,
            today=today,
        )
        if eff.ambiguous:
            findings.append(
                _policy_finding(
                    CheckId.policy_invalid,
                    qualifier=_hash("ambiguous", str(router.router_id)),
                    title=f"{router.name}: more than one policy entry matches this router",
                    impact="Defaults (strictest) apply until the policy is fixed.",
                    evidence={
                        "matching_entries": len(
                            matching_entries(
                                policy,
                                identity=doc.identity,
                                router_name=router.name,
                                router_id=router.router_id,
                            )
                        )
                    },
                    fix="Make the router-health policy entries for this router unambiguous.",
                    router_id=router.router_id,
                )
            )
        for lapsed in eff.lapsed:
            days = (today - lapsed.until).days
            if 0 < days <= EXPIRED_NOTE_DAYS:
                findings.append(
                    _policy_finding(
                        CheckId.policy_exception_expired,
                        qualifier=_hash(
                            str(router.router_id),
                            lapsed.kind,
                            lapsed.check,
                            lapsed.scope,
                        ),
                        title=(
                            f"{router.name}: policy {lapsed.kind} {lapsed.check} "
                            f"lapsed on {lapsed.until.isoformat()}"
                        ),
                        impact="The check now evaluates normally; review the exception.",
                        evidence={
                            "kind": lapsed.kind,
                            "check": lapsed.check,
                            "scope": lapsed.scope,
                            "until": lapsed.until.isoformat(),
                        },
                        fix="Extend the dated exception or fix the underlying issue.",
                        router_id=router.router_id,
                    )
                )
        result = evaluate_posture(
            doc,
            eff,
            router_label=router.name,
            suspended_list=suspended_list,
            has_pppoe_or_nas=router.linked_to_active_nas,
        )
        for pf in result.findings:
            findings.append(_health(pf, router=router, snapshot=snapshot))
        for check in sorted(result.evaluated_checks, key=lambda c: c.value):
            scopes.append(
                findings_service.Scope(HealthLayer.posture, router.router_id, check)
            )

    # ---- policy-layer findings (policy itself, not any one router)
    if loaded_policy.error:
        findings.append(
            _policy_finding(
                CheckId.policy_invalid,
                qualifier="stored",
                title="Router health policy is invalid; built-in defaults are in use",
                impact="Every router is judged with the strictest defaults and no exceptions.",
                evidence={"error": loaded_policy.error[:200]},
                fix="Correct the router-health policy setting.",
            )
        )
    if all(identity is not None for identity, _name, _id in identities):
        for entry in unmatched_entries(policy, identities):
            match = entry.match
            findings.append(
                _policy_finding(
                    CheckId.policy_unmatched,
                    qualifier=_hash(
                        match.identity or "",
                        match.router_name or "",
                        str(match.router_id or ""),
                    ),
                    title="A router-health policy entry matches no active router",
                    impact="The entry has no effect; the router may have been renamed.",
                    evidence={
                        "match_identity": match.identity,
                        "match_router_name": match.router_name,
                    },
                    fix="Update or remove the stale policy entry.",
                )
            )
    for check in POLICY_CHECKS:
        scopes.append(
            findings_service.Scope(HealthLayer.policy, POLICY_SUBJECT_ID, check)
        )

    # ---- cap, carrying forward anything cut
    kept, dropped = findings_service.cap_findings(
        findings, thresholds.max_findings_per_run
    )
    if dropped:
        kept_ids = {id(f) for f in kept}
        cut = {
            (f.layer, f.subject_id, f.check) for f in findings if id(f) not in kept_ids
        }
        scopes = [s for s in scopes if (s.layer, s.subject_id, s.check) not in cut]
        kept.append(
            _policy_finding(
                CheckId.policy_overflow,
                qualifier="0",
                title="Router health produced more findings than the per-run cap",
                impact=f"{dropped} lower-priority findings were not recorded this run.",
                evidence={"dropped": dropped, "cap": thresholds.max_findings_per_run},
                fix="Raise router_health_max_findings_per_run or fix the top findings.",
            )
        )

    # ---- routers that are no longer active resolve everything they had open
    retire: list[tuple[HealthLayer, UUID]] = []
    active_ids = {r.router_id for r in routers}
    for layer in (HealthLayer.posture, HealthLayer.snapshot):
        for subject in (open_subjects or {}).get(layer, set()) - active_ids:
            retire.append((layer, subject))

    return PostureComputation(
        findings=tuple(kept),
        scopes=tuple(scopes),
        retire_subjects=tuple(retire),
        capped=dropped,
        routers=len(routers),
        parsed=parsed,
        unparsed=unparsed,
        no_snapshot=no_snapshot,
    )


__all__ = [
    "POLICY_SUBJECT_ID",
    "ExportSignalInput",
    "PostureComputation",
    "PostureThresholds",
    "RouterInput",
    "SnapshotInput",
    "compute_posture",
]
