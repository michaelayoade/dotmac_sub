"""Daily no-op CoA canary (layer 3): proves a NAS answers CoA, safely.

A canary is one Disconnect-Request carrying ONLY a synthetic ``User-Name`` that
matches no account, so no real session can be touched. It uses the extracted
exchange function (``enforcement.send_coa_probe_disconnect``): no negative-cache
read or write, no enforcement evidence. Healthy results are NAKs
(``rejected`` / ``session_not_found``); an ACK is critical.

Order of operations: select targets (read) -> commit audit ``started`` ->
send sequentially with spacing -> record ``canary`` buckets -> re-probe timeouts
once -> commit audit ``completed`` and sync findings.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import AccessCredential, NasDevice
from app.models.radius import RadiusUser
from app.models.router_health import NasCoaOutcomeBucket
from app.services import control_registry, enforcement
from app.services.radius_session_reconcile import external_radacct_username_seen
from app.services.router_health import commands, reconciler, service_checks
from app.services.router_health import findings as findings_service
from app.services.router_health.coa_evidence import record_coa_outcome
from app.services.router_health.contracts import (
    CanaryDisposition,
    CanaryResult,
    CanaryTarget,
    CheckId,
    CoaKind,
    HealthLayer,
)
from app.services.router_health.policy import (
    LoadedPolicy,
    effective_policy,
    load_policy,
    today_in_app_timezone,
)
from app.services.router_health.service_path import NasInput, nas_label, to_nas_finding
from app.services.router_health.settings import RouterHealthSettings, load_settings

logger = logging.getLogger(__name__)

CANARY_PREFIX = "dotmac-coa-canary-"
CONTROL_COA = "access.radius_coa"
CANARY_FINDING_CHECKS = (
    CheckId.coa_canary_timeout,
    CheckId.coa_canary_acked,
    CheckId.coa_canary_config,
)
_FAILED = frozenset({CanaryDisposition.timeout, CanaryDisposition.transport_error})
# Dispositions stored in nas_coa_outcome_buckets (a collision sends nothing).
_NOT_RECORDED = frozenset(
    {
        CanaryDisposition.aborted_collision,
        CanaryDisposition.collision_check_unavailable,
    }
)

# Leave headroom under the task soft limit (15 minutes) for the closing commit.
CANARY_RUN_BUDGET_SECONDS = 12 * 60

ReprobeScheduler = Callable[[UUID, UUID, int], None]


def canary_username() -> str:
    """Unique per packet; the prefix is not producible by PPPoE generation."""
    return f"{CANARY_PREFIX}{secrets.token_hex(8)}"


def username_collides(db: Session, username: str) -> bool:
    """True if a local credential or RADIUS user row uses the name.

    The external radacct check is separate (``external_radacct_username_seen``)
    because it can fail and must then stop the probe rather than pass it.
    """
    if db.scalar(
        select(func.count())
        .select_from(AccessCredential)
        .where(AccessCredential.username == username)
    ):
        return True
    return bool(
        db.scalar(
            select(func.count())
            .select_from(RadiusUser)
            .where(RadiusUser.username == username)
        )
    )


@dataclass(frozen=True)
class CanaryRunStats:
    targets: int = 0
    healthy: int = 0
    failed_pending_reprobe: int = 0
    findings: int = 0
    skipped: str | None = None
    run_id: UUID | None = None
    deferred: int = 0


def _group_targets(nas_rows: list[NasDevice]) -> list[CanaryTarget]:
    """Deduplicate by (host, coa_port); a result applies to every row on it."""
    groups: dict[tuple[str, int], list[NasDevice]] = {}
    for nas in nas_rows:
        host = reconciler.nas_host(nas)
        if not host:
            continue
        groups.setdefault((str(host), int(nas.coa_port or 3799)), []).append(nas)
    targets: list[CanaryTarget] = []
    for (host, port), rows in sorted(groups.items()):
        # Enforcement resolves the CoA secret from the row's local shared_secret
        # first (then the external nas table), so the probing row is one that
        # has a local secret; ties break by id for a stable choice.
        ordered = sorted(rows, key=lambda n: (not n.shared_secret, str(n.id)))
        targets.append(
            CanaryTarget(
                host=host,
                coa_port=port,
                nas_device_ids=tuple(n.id for n in ordered),
                primary_nas_device_id=ordered[0].id,
            )
        )
    return targets


def _checks_off_coa_canary(
    db: Session,
    loaded: LoadedPolicy,
    nas: NasDevice,
    routers_by_ip: dict[str, tuple[UUID, str]],
    identities: dict[UUID, str | None],
) -> bool:
    host = reconciler.nas_host(nas)
    router = routers_by_ip.get(str(host)) if host else None
    if router is None:
        return False
    eff = effective_policy(
        loaded.policy,
        identity=identities.get(router[0]),
        router_name=router[1],
        router_id=router[0],
        today=today_in_app_timezone(),
    )
    return eff.check_off("coa-canary")


def select_targets(
    db: Session,
    *,
    settings: RouterHealthSettings,
    loaded: LoadedPolicy,
    now: datetime,
) -> list[CanaryTarget]:
    """Eligible, rate-limited, de-duplicated, capped canary targets."""
    from app.models.router_management import Router

    routers_by_ip = {
        str(ip): (rid, name)
        for rid, name, ip in db.execute(
            select(Router.id, Router.name, Router.management_ip).where(
                Router.is_active.is_(True)
            )
        )
    }
    identities = reconciler.router_identities(db)
    eligible = [
        nas
        for nas in reconciler.canary_eligible_nas(db)
        if not _checks_off_coa_canary(db, loaded, nas, routers_by_ip, identities)
    ]
    spacing_cutoff = now - timedelta(hours=settings.coa_canary_min_spacing_hours)
    recent: set[UUID] = {
        nas_id
        for (nas_id,) in db.execute(
            select(NasCoaOutcomeBucket.nas_device_id)
            .where(
                NasCoaOutcomeBucket.kind == "canary",
                NasCoaOutcomeBucket.last_at >= spacing_cutoff,
            )
            .group_by(NasCoaOutcomeBucket.nas_device_id)
        )
    }
    targets = [
        t for t in _group_targets(eligible) if not (set(t.nas_device_ids) & recent)
    ]
    return targets[: settings.coa_canary_max_targets]


def send_canary(db: Session, target: CanaryTarget) -> CanaryDisposition:
    """One probe for one target (read-only on the database)."""
    nas = db.get(NasDevice, target.primary_nas_device_id)
    if nas is None:
        return CanaryDisposition.missing_nas_host
    username = canary_username()
    if username_collides(db, username):
        return CanaryDisposition.aborted_collision
    external = external_radacct_username_seen(db, username)
    if external is None:
        # Fail safe: if uniqueness cannot be proven, send nothing.
        return CanaryDisposition.collision_check_unavailable
    if external:
        return CanaryDisposition.aborted_collision
    disposition = enforcement.send_coa_probe_disconnect(db, nas, username=username)
    return CanaryDisposition(disposition.value)


def _nas_inputs(db: Session, nas_ids: tuple[UUID, ...]) -> list[NasInput]:
    from app.models.router_management import Router

    routers_by_ip = {
        str(ip): (rid, name)
        for rid, name, ip in db.execute(
            select(Router.id, Router.name, Router.management_ip).where(
                Router.is_active.is_(True)
            )
        )
    }
    rows: list[NasInput] = []
    for nas in db.execute(select(NasDevice).where(NasDevice.id.in_(nas_ids))).scalars():
        host = reconciler.nas_host(nas)
        router = routers_by_ip.get(str(host)) if host else None
        rows.append(
            NasInput(
                nas_device_id=nas.id,
                name=nas.name,
                router_id=router[0] if router else None,
                router_name=router[1] if router else None,
                canary_eligible=True,
                last_canary_at=None,
                coa_counts={},
                enforcement_failures=[],
            )
        )
    return rows


def _build_outputs(
    nas_rows: list[NasInput],
    results: list[CanaryResult],
) -> tuple[list, list, list[tuple[UUID, CanaryDisposition]]]:
    by_nas = {n.nas_device_id: n for n in nas_rows}
    findings = []
    scopes = []
    dispositions: list[tuple[UUID, CanaryDisposition]] = []
    for result in results:
        pending = result.disposition in _FAILED and not result.reprobe
        for nas_id in result.target.nas_device_ids:
            nas = by_nas.get(nas_id)
            if nas is None:
                continue
            dispositions.append((nas_id, result.disposition))
            if pending:
                continue  # carried forward until the re-probe answers
            for pf in service_checks.evaluate_canary_result(
                nas_label(nas.name, nas.router_name),
                result.disposition,
                reprobe_failed=result.reprobe,
            ):
                findings.append(to_nas_finding(pf, nas))
            for check in CANARY_FINDING_CHECKS:
                scopes.append(
                    findings_service.Scope(HealthLayer.service, nas_id, check)
                )
    return findings, scopes, dispositions


def _record_buckets(result: CanaryResult) -> None:
    if result.disposition in _NOT_RECORDED:
        return
    for nas_id in result.target.nas_device_ids:
        record_coa_outcome(nas_id, CoaKind.canary, result.disposition.value)


def run_coa_canary(
    db: Session,
    *,
    task_name: str,
    schedule_reprobe: ReprobeScheduler,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    budget_seconds: float = CANARY_RUN_BUDGET_SECONDS,
) -> CanaryRunStats:
    moment = now or datetime.now(UTC)
    settings = load_settings(db)
    loaded = load_policy(db)
    if not control_registry.is_enabled(db, CONTROL_COA):
        db.rollback()
        return CanaryRunStats(skipped="coa_disabled")
    targets = select_targets(db, settings=settings, loaded=loaded, now=moment)
    all_ids = tuple(nas_id for t in targets for nas_id in t.nas_device_ids)
    nas_rows = _nas_inputs(db, all_ids) if all_ids else []
    db.rollback()
    if not targets:
        return CanaryRunStats(skipped="no_targets")

    run_id = uuid4()
    commands.begin_coa_canary_run(
        db,
        command=commands.BeginCanaryRunCommand(
            context=commands.system_context(task_name),
            run_id=run_id,
            targets=tuple(
                commands.CanaryTargetSummary(
                    nas_device_ids=t.nas_device_ids, host=t.host
                )
                for t in targets
            ),
            username_prefix=CANARY_PREFIX,
        ),
    )
    results: list[CanaryResult] = []
    spacing = settings.coa_canary_spacing_ms / 1000.0
    started = monotonic()
    deferred = 0
    for index, target in enumerate(targets):
        if index:
            # Stop scheduling new probes when less than one spacing interval
            # of the run budget is left; deferred targets stay eligible for
            # the next run (no bucket was written for them).
            if budget_seconds - (monotonic() - started) < spacing:
                deferred = len(targets) - index
                break
            sleep(spacing)
        disposition = send_canary(db, target)
        db.rollback()
        result = CanaryResult(target=target, disposition=disposition)
        _record_buckets(result)
        results.append(result)
        if disposition in _FAILED:
            schedule_reprobe(
                target.primary_nas_device_id,
                run_id,
                settings.coa_canary_reprobe_seconds,
            )
    findings, scopes, dispositions = _build_outputs(nas_rows, results)
    outcome = commands.complete_coa_canary_run(
        db,
        command=commands.CompleteCanaryRunCommand(
            context=commands.system_context(task_name),
            run_id=run_id,
            dispositions=tuple(dispositions),
            findings=tuple(findings),
            scopes=tuple(scopes),
            mode=settings.alert_mode,
        ),
    )
    _ = outcome
    pending = sum(1 for r in results if r.disposition in _FAILED)
    return CanaryRunStats(
        targets=len(results),
        healthy=sum(
            1 for r in results if r.disposition in service_checks.HEALTHY_CANARY
        ),
        failed_pending_reprobe=pending,
        findings=len(findings),
        run_id=run_id,
        deferred=deferred,
    )


def reprobe_coa_canary(
    db: Session,
    *,
    nas_device_id: UUID,
    run_id: UUID,
    task_name: str,
) -> CanaryRunStats:
    """Second, final probe after a timeout; only a repeated failure alerts."""
    settings = load_settings(db)
    if not control_registry.is_enabled(db, CONTROL_COA):
        db.rollback()
        return CanaryRunStats(skipped="coa_disabled", run_id=run_id)
    nas = db.get(NasDevice, nas_device_id)
    if nas is None or not nas.is_active:
        db.rollback()
        return CanaryRunStats(skipped="nas_gone", run_id=run_id)
    host = reconciler.nas_host(nas)
    port = int(nas.coa_port or 3799)
    same_host = [
        n
        for n in db.execute(
            select(NasDevice).where(NasDevice.is_active.is_(True))
        ).scalars()
        if reconciler.nas_host(n) == host and int(n.coa_port or 3799) == port
    ]
    targets = _group_targets(same_host)
    target = next((t for t in targets if nas_device_id in t.nas_device_ids), None)
    if target is None:
        db.rollback()
        return CanaryRunStats(skipped="no_target", run_id=run_id)
    nas_rows = _nas_inputs(db, target.nas_device_ids)
    disposition = send_canary(db, target)
    db.rollback()
    result = CanaryResult(target=target, disposition=disposition, reprobe=True)
    _record_buckets(result)
    findings, scopes, dispositions = _build_outputs(nas_rows, [result])
    commands.complete_coa_canary_run(
        db,
        command=commands.CompleteCanaryRunCommand(
            context=commands.system_context(task_name),
            run_id=run_id,
            dispositions=tuple(dispositions),
            findings=tuple(findings),
            scopes=tuple(scopes),
            mode=settings.alert_mode,
            reprobe=True,
        ),
    )
    return CanaryRunStats(
        targets=1,
        healthy=1 if disposition in service_checks.HEALTHY_CANARY else 0,
        findings=len(findings),
        run_id=run_id,
    )
