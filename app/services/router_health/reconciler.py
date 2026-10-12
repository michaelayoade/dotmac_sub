"""Database adapters for the router health reconciler (read, compute, command).

Each entry point reads on the caller's session, releases that read transaction,
computes purely, then enters exactly one owner command on a transaction-free
session. No router is contacted here.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.admin_alert import AdminAlert
from app.models.catalog import NasDevice, NasVendor
from app.models.enforcement_application import EnforcementApplication
from app.models.network_monitoring import AlertStatus
from app.models.router_health import NasCoaOutcomeBucket, RouterHealthSignal
from app.models.router_management import Router, RouterConfigSnapshot
from app.services import control_registry
from app.services.radius_address_lists import suspended_address_list
from app.services.router_health import commands
from app.services.router_health import posture as posture_service
from app.services.router_health import service_path as service_path_service
from app.services.router_health.contracts import (
    FINGERPRINT_ROOT,
    ConfigExportOutcome,
    HealthLayer,
)
from app.services.router_health.policy import (
    LoadedPolicy,
    effective_policy,
    load_policy,
    today_in_app_timezone,
)
from app.services.router_health.routeros_export import parse_export
from app.services.router_health.service_checks import EnforcementFailureRow
from app.services.router_health.settings import RouterHealthSettings, load_settings

logger = logging.getLogger(__name__)

CONTROL_POSTURE = "network.router_health_posture"
CONTROL_CANARY = "network.router_health_coa_canary"
BUCKET_RETENTION_DAYS = 35
_COA_KINDS = ("disconnect", "update")


@dataclass(frozen=True)
class RunStats:
    subjects: int
    findings: int
    opened: int
    resolved: int
    capped: int = 0
    unparsed: int = 0


def nas_host(nas: NasDevice) -> str | None:
    """The host enforcement sends CoA to (same rule as ``enforcement``)."""
    return nas.nas_ip or nas.management_ip or nas.ip_address


def _open_subjects(db: Session, layer: HealthLayer) -> set[UUID]:
    prefix = f"{FINGERPRINT_ROOT}{layer.value}:"
    subjects: set[UUID] = set()
    for (fingerprint,) in db.execute(
        select(AdminAlert.fingerprint).where(
            AdminAlert.fingerprint.like(f"{prefix}%"),
            AdminAlert.status != AlertStatus.resolved,
        )
    ):
        parts = fingerprint.split(":")
        # network:router-health:<layer>:<uuid>:<check>:<qualifier>
        if len(parts) >= 4:
            try:
                subjects.add(UUID(parts[3]))
            except ValueError:
                continue
    return subjects


def _latest_snapshots(db: Session) -> dict[UUID, posture_service.SnapshotInput]:
    newest = (
        select(
            RouterConfigSnapshot.router_id.label("router_id"),
            func.max(RouterConfigSnapshot.created_at).label("created_at"),
        )
        .group_by(RouterConfigSnapshot.router_id)
        .subquery()
    )
    rows = db.execute(
        select(
            RouterConfigSnapshot.router_id,
            RouterConfigSnapshot.id,
            RouterConfigSnapshot.created_at,
            RouterConfigSnapshot.config_export,
        ).join(
            newest,
            (RouterConfigSnapshot.router_id == newest.c.router_id)
            & (RouterConfigSnapshot.created_at == newest.c.created_at),
        )
    ).all()
    latest: dict[UUID, posture_service.SnapshotInput] = {}
    for router_id, snapshot_id, created_at, text in rows:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        latest[router_id] = posture_service.SnapshotInput(
            snapshot_id=snapshot_id, created_at=created_at, config_export=text
        )
    return latest


def _export_signals(db: Session) -> dict[UUID, posture_service.ExportSignalInput]:
    rows = db.execute(
        select(RouterHealthSignal).where(
            RouterHealthSignal.signal == commands.SIGNAL_CONFIG_EXPORT,
            RouterHealthSignal.scope == commands.SCOPE_ROUTER,
        )
    ).scalars()
    out: dict[UUID, posture_service.ExportSignalInput] = {}
    for row in rows:
        observed = row.last_observed_at
        if observed is not None and observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        out[row.router_id] = posture_service.ExportSignalInput(
            status=row.status,
            failure_class=row.last_text,
            consecutive_failures=int(row.consecutive_abnormal or 0),
            last_attempt_at=observed,
        )
    return out


def _active_nas_hosts(db: Session) -> set[str]:
    hosts: set[str] = set()
    for nas in db.execute(
        select(NasDevice).where(NasDevice.is_active.is_(True))
    ).scalars():
        for value in (nas.nas_ip, nas.management_ip, nas.ip_address):
            if value:
                hosts.add(str(value))
    return hosts


def reconcile_router_posture(
    db: Session, *, task_name: str, now: datetime | None = None
) -> RunStats:
    """Layer 1: snapshot staleness and posture (database-only)."""
    moment = now or datetime.now(UTC)
    settings = load_settings(db)
    loaded = load_policy(db)
    suspended = suspended_address_list(db)
    snapshots = _latest_snapshots(db)
    signals = _export_signals(db)
    nas_hosts = _active_nas_hosts(db)
    router_rows = db.execute(
        select(Router.id, Router.name, Router.management_ip)
        .where(Router.is_active.is_(True))
        .order_by(Router.name)
    ).all()
    open_subjects = {
        HealthLayer.posture: _open_subjects(db, HealthLayer.posture),
        HealthLayer.snapshot: _open_subjects(db, HealthLayer.snapshot),
    }
    db.rollback()  # end the read transaction before the owner command
    routers = [
        posture_service.RouterInput(
            router_id=rid,
            name=name,
            management_ip=ip,
            snapshot=snapshots.get(rid),
            export_signal=signals.get(rid),
            linked_to_active_nas=str(ip) in nas_hosts,
        )
        for rid, name, ip in router_rows
    ]
    computation = posture_service.compute_posture(
        routers=routers,
        loaded_policy=loaded,
        suspended_list=suspended,
        thresholds=posture_service.PostureThresholds(
            snapshot_max_age_hours=settings.snapshot_max_age_hours,
            snapshot_high_age_hours=settings.snapshot_high_age_hours,
            max_findings_per_run=settings.max_findings_per_run,
        ),
        now=moment,
        today=today_in_app_timezone(moment),
        open_subjects=open_subjects,
    )
    outcome = commands.reconcile_router_posture(
        db,
        command=commands.ProjectFindingsCommand(
            context=commands.system_context(task_name),
            findings=computation.findings,
            scopes=computation.scopes,
            mode=settings.alert_mode,
            retire_subjects=computation.retire_subjects,
            capped=computation.capped,
        ),
    )
    return RunStats(
        subjects=computation.routers,
        findings=len(computation.findings),
        opened=outcome.opened,
        resolved=outcome.resolved,
        capped=computation.capped,
        unparsed=computation.unparsed,
    )


def record_config_export_outcomes(
    db: Session, *, outcomes: list[ConfigExportOutcome], task_name: str
) -> int:
    """Persist one snapshot run's typed outcomes (transaction-free session)."""
    if not outcomes:
        return 0
    return commands.record_config_export_outcomes(
        db,
        command=commands.RecordConfigExportOutcomesCommand(
            context=commands.system_context(task_name), outcomes=tuple(outcomes)
        ),
    )


def canary_eligible_nas(db: Session) -> list[NasDevice]:
    """Active MikroTik NAS rows with a resolvable host."""
    rows = db.execute(
        select(NasDevice).where(
            NasDevice.is_active.is_(True), NasDevice.vendor == NasVendor.mikrotik
        )
    ).scalars()
    return [nas for nas in rows if nas_host(nas)]


def router_identities(db: Session) -> dict[UUID, str | None]:
    identities: dict[UUID, str | None] = {}
    for rid, snap in _latest_snapshots(db).items():
        identities[rid] = parse_export(snap.config_export).identity
    return identities


def _nas_annotations(
    db: Session, loaded: LoadedPolicy, linked: dict[UUID, UUID | None]
) -> dict[UUID, dict[str, str]]:
    identities = router_identities(db)
    names: dict[UUID, str] = {
        rid: name for rid, name in db.execute(select(Router.id, Router.name))
    }
    today = today_in_app_timezone()
    notes: dict[UUID, dict[str, str]] = {}
    for nas_id, router_id in linked.items():
        if router_id is None:
            continue
        eff = effective_policy(
            loaded.policy,
            identity=identities.get(router_id),
            router_name=names.get(router_id, ""),
            router_id=router_id,
            today=today,
        )
        if eff.annotations:
            notes[nas_id] = dict(eff.annotations)
    return notes


def reconcile_service_path(
    db: Session, *, task_name: str, now: datetime | None = None
) -> RunStats:
    """Layer 3: CoA outcome rates, enforcement-path failures, canary staleness."""
    moment = now or datetime.now(UTC)
    settings = load_settings(db)
    loaded = load_policy(db)
    canary_enabled = control_registry.is_enabled(db, CONTROL_CANARY)
    nas_devices = list(
        db.execute(select(NasDevice).where(NasDevice.is_active.is_(True))).scalars()
    )
    routers_by_ip: dict[str, tuple[UUID, str]] = {
        str(ip): (rid, name)
        for rid, name, ip in db.execute(
            select(Router.id, Router.name, Router.management_ip).where(
                Router.is_active.is_(True)
            )
        )
    }
    since = moment - timedelta(hours=settings.coa_window_hours)
    counts: dict[UUID, dict[str, int]] = defaultdict(dict)
    for nas_id, disposition, total in db.execute(
        select(
            NasCoaOutcomeBucket.nas_device_id,
            NasCoaOutcomeBucket.disposition,
            func.sum(NasCoaOutcomeBucket.count),
        )
        .where(
            NasCoaOutcomeBucket.kind.in_(_COA_KINDS),
            NasCoaOutcomeBucket.bucket_start
            >= since.replace(minute=0, second=0, microsecond=0),
        )
        .group_by(NasCoaOutcomeBucket.nas_device_id, NasCoaOutcomeBucket.disposition)
    ):
        counts[nas_id][disposition] = int(total or 0)
    last_canary: dict[UUID, datetime] = {}
    for nas_id, last_at in db.execute(
        select(NasCoaOutcomeBucket.nas_device_id, func.max(NasCoaOutcomeBucket.last_at))
        .where(NasCoaOutcomeBucket.kind == "canary")
        .group_by(NasCoaOutcomeBucket.nas_device_id)
    ):
        last_canary[nas_id] = (
            last_at.replace(tzinfo=UTC) if last_at.tzinfo is None else last_at
        )
    failures: dict[UUID, list[EnforcementFailureRow]] = defaultdict(list)
    for nas_id, path, failure_class, total in db.execute(
        select(
            EnforcementApplication.nas_device_id,
            EnforcementApplication.path,
            EnforcementApplication.failure_class,
            func.count(),
        )
        .where(
            EnforcementApplication.outcome == "failed",
            EnforcementApplication.last_attempt_at >= since,
        )
        .group_by(
            EnforcementApplication.nas_device_id,
            EnforcementApplication.path,
            EnforcementApplication.failure_class,
        )
    ):
        failures[nas_id].append(
            EnforcementFailureRow(
                path=path or "unknown",
                failure_class=failure_class or "unknown",
                count=int(total),
            )
        )
    eligible_ids = {n.id for n in canary_eligible_nas(db)}
    linked: dict[UUID, UUID | None] = {}
    nas_inputs: list[service_path_service.NasInput] = []
    for nas in nas_devices:
        host = nas_host(nas)
        router = routers_by_ip.get(str(host)) if host else None
        linked[nas.id] = router[0] if router else None
        nas_inputs.append(
            service_path_service.NasInput(
                nas_device_id=nas.id,
                name=nas.name,
                router_id=router[0] if router else None,
                router_name=router[1] if router else None,
                canary_eligible=nas.id in eligible_ids,
                last_canary_at=last_canary.get(nas.id),
                coa_counts=counts.get(nas.id, {}),
                enforcement_failures=failures.get(nas.id, []),
            )
        )
    annotations = _nas_annotations(db, loaded, linked)
    open_subjects = _open_subjects(db, HealthLayer.service)
    db.rollback()
    computation = service_path_service.compute_service_path(
        nas_rows=nas_inputs,
        settings=settings,
        canary_enabled=canary_enabled,
        now=moment,
        annotations=annotations,
        open_subjects=open_subjects,
    )
    outcome = commands.reconcile_service_path(
        db,
        command=commands.ProjectFindingsCommand(
            context=commands.system_context(task_name),
            findings=computation.findings,
            scopes=computation.scopes,
            mode=settings.alert_mode,
            retire_subjects=computation.retire_subjects,
        ),
    )
    from app.services.router_health import coa_evidence

    coa_evidence.prune_coa_outcome_buckets(retention_days=BUCKET_RETENTION_DAYS)
    return RunStats(
        subjects=computation.nas_count,
        findings=len(computation.findings),
        opened=outcome.opened,
        resolved=outcome.resolved,
    )


def settings_for(db: Session) -> RouterHealthSettings:
    return load_settings(db)
