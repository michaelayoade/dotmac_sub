"""Layer 2 adapter: periodic read-only REST poll of every active router.

Order of work (network I/O never happens inside the owner transaction):

1. read routers, prior signal state, policy and settings on the caller's session,
   detach the routers, and end that read transaction;
2. poll routers concurrently (bounded workers, per-router deadline) with the
   GET-only reader; no database access happens in the worker threads;
3. evaluate purely, then enter exactly one ``record_runtime_poll`` owner command.

The runtime layer never writes ``Router.status`` or ``last_seen_at``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.router_health import RouterHealthSignal
from app.models.router_management import Router, RouterStatus
from app.services import control_registry
from app.services.router_health import commands, reconciler
from app.services.router_health import findings as findings_service
from app.services.router_health.contracts import (
    HealthFinding,
    HealthLayer,
    RuntimeObservation,
    SignalState,
)
from app.services.router_health.policy import (
    effective_policy,
    load_policy,
    today_in_app_timezone,
)
from app.services.router_health.runtime_checks import (
    RUNTIME_SIGNALS,
    RuntimeRouter,
    evaluate_runtime,
    optics_due,
)
from app.services.router_health.runtime_reader import (
    ReaderLimits,
    RouterOSRuntimeReader,
)
from app.services.router_health.settings import RouterHealthSettings, load_settings

logger = logging.getLogger(__name__)

CONTROL_RUNTIME = "network.router_health_runtime_poll"
CONTROL_OPTICS = "network.router_health_optics"


class RuntimeReader(Protocol):
    def poll(
        self, router: Router, *, router_id: UUID, include_optics: bool
    ) -> RuntimeObservation: ...


@dataclass(frozen=True)
class RuntimeRunStats:
    polled: int
    failed: int
    skipped: int
    findings: int
    opened: int
    resolved: int
    capped: int = 0


def _as_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _utc(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def _prior_signals(
    db: Session, router_ids: list[UUID]
) -> dict[UUID, dict[tuple[str, str], SignalState]]:
    prior: dict[UUID, dict[tuple[str, str], SignalState]] = defaultdict(dict)
    if not router_ids:
        return prior
    rows = db.execute(
        select(RouterHealthSignal).where(
            RouterHealthSignal.router_id.in_(router_ids),
            RouterHealthSignal.signal.in_(RUNTIME_SIGNALS),
        )
    ).scalars()
    for row in rows:
        observed = _utc(row.last_observed_at) or datetime.now(UTC)
        prior[row.router_id][(row.signal, row.scope)] = SignalState(
            signal=row.signal,
            scope=row.scope,
            status=row.status,
            last_value=_as_float(row.last_value),
            last_text=row.last_text,
            samples=tuple((int(ts), float(v)) for ts, v in (row.samples or [])),
            first_abnormal_at=_utc(row.first_abnormal_at),
            last_normal_at=_utc(row.last_normal_at),
            last_observed_at=observed,
            consecutive_abnormal=int(row.consecutive_abnormal or 0),
            boot_marker=row.boot_marker,
        )
    return prior


def _limits(settings: RouterHealthSettings) -> ReaderLimits:
    return ReaderLimits(
        connect_timeout=float(settings.runtime_connect_timeout_seconds),
        read_timeout=float(settings.runtime_read_timeout_seconds),
        router_deadline=float(settings.runtime_router_deadline_seconds),
    )


def poll_router_runtime(
    db: Session,
    *,
    task_name: str,
    now: datetime | None = None,
    reader: RuntimeReader | None = None,
) -> RuntimeRunStats:
    """Layer 2: poll, evaluate and project runtime health (see module docstring)."""
    moment = now or datetime.now(UTC)
    settings = load_settings(db)
    loaded = load_policy(db)
    optics_enabled = control_registry.is_enabled(db, CONTROL_OPTICS)
    rows = list(
        db.execute(
            select(Router)
            .options(selectinload(Router.jump_host))
            .where(
                Router.is_active.is_(True),
                Router.status != RouterStatus.maintenance,
            )
            .order_by(Router.name)
        ).scalars()
    )
    pollable = [r for r in rows if r.rest_api_username and r.rest_api_password]
    skipped = len(rows) - len(pollable)
    identities = reconciler.router_identities(db)
    prior = _prior_signals(db, [r.id for r in pollable])
    active_ids = {
        r.id
        for r in db.execute(select(Router).where(Router.is_active.is_(True))).scalars()
    }
    open_subjects = reconciler.open_subjects(db, HealthLayer.runtime)
    meta = [(r.id, r.name, r.rest_api_username) for r in pollable]
    for router in pollable:
        if router.jump_host is not None:
            db.expunge(router.jump_host)
        db.expunge(router)
    db.rollback()  # end the read transaction before any network I/O

    poller: RuntimeReader = reader or RouterOSRuntimeReader(_limits(settings))
    due = {
        rid: optics_enabled and optics_due(prior.get(rid, {}), moment)
        for rid, _, _ in meta
    }

    def poll_one(router: Router) -> RuntimeObservation:
        return poller.poll(router, router_id=router.id, include_optics=due[router.id])

    workers = max(1, min(settings.runtime_concurrency, len(pollable) or 1))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="rh-runtime"
    ) as pool:
        observations = list(pool.map(poll_one, pollable))

    today = today_in_app_timezone(moment)
    findings: list[HealthFinding] = []
    scopes: list[findings_service.Scope] = []
    results: list[commands.RouterRuntimeResult] = []
    failed = 0
    for (rid, name, username), observation in zip(meta, observations, strict=True):
        eff = effective_policy(
            loaded.policy,
            identity=identities.get(rid),
            router_name=name,
            router_id=rid,
            today=today,
        )
        evaluation = evaluate_runtime(
            router=RuntimeRouter(router_id=rid, name=name, rest_username=username),
            observation=observation,
            prior=prior.get(rid, {}),
            policy=eff,
            settings=settings,
            now=moment,
            optics_enabled=optics_enabled,
        )
        failed += 1 if observation.failure is not None else 0
        findings.extend(evaluation.findings)
        scopes.extend(
            findings_service.Scope(HealthLayer.runtime, rid, check)
            for check in sorted(evaluation.judged, key=lambda c: c.value)
        )
        results.append(
            commands.RouterRuntimeResult(
                router_id=rid,
                signals=tuple(evaluation.signals),
                retire=tuple(evaluation.retire),
            )
        )

    kept, dropped = findings_service.cap_findings(
        findings, settings.max_findings_per_run
    )
    if dropped:
        # Carry forward anything whose findings were cut so it is never resolved
        # without having been judged in full.
        cut = {(f.subject_id, f.check) for f in findings} - {
            (f.subject_id, f.check) for f in kept
        }
        scopes = [s for s in scopes if (s.subject_id, s.check) not in cut]
    inactive = tuple(
        (HealthLayer.runtime, sid) for sid in sorted(open_subjects - active_ids)
    )
    outcome = commands.record_runtime_poll(
        db,
        command=commands.RecordRuntimePollCommand(
            context=commands.system_context(task_name),
            results=tuple(results),
            findings=tuple(kept),
            scopes=tuple(scopes),
            mode=settings.alert_mode,
            retire_subjects=inactive,
            capped=dropped,
        ),
    )
    return RuntimeRunStats(
        polled=len(pollable),
        failed=failed,
        skipped=skipped,
        findings=len(kept),
        opened=outcome.opened,
        resolved=outcome.resolved,
        capped=dropped,
    )


__all__ = [
    "CONTROL_OPTICS",
    "CONTROL_RUNTIME",
    "RuntimeRunStats",
    "poll_router_runtime",
]
