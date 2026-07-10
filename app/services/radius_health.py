"""RADIUS health monitor (operations strategy priority 2).

Customer experience often fails before a router does: accounting stops
flowing, sessions go stale, or enforcement drifts (suspended customers still
online). This service derives those signals from the external radacct DB and
the reconciled ``radius_active_sessions`` view, pushes the trend series to
VictoriaMetrics, and hands the latest counters to the admin-alert evaluator
via the shared task heartbeat.

Deliberately DB-derived only in this phase — synthetic auth probes (RTT,
timeout/resend counters from the server's point of view) need a RADIUS
protocol client and land separately.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

HEARTBEAT_TASK = "radius_health"

# Advisory-lock key for the single-flight health task ("rHl").
ADVISORY_LOCK_KEY = 0x72_48_6C

# An OPEN radacct session whose interim update is older than this is stale —
# either the NAS stopped sending accounting or the session died without a
# Stop. Default 3x a typical 5-minute interim interval.
DEFAULT_STALE_SESSION_SECONDS = 900

_vm_writer = None


def _writer():
    global _vm_writer
    if _vm_writer is None:
        from app.services.bandwidth_metrics_adapter import VictoriaMetricsWriter

        _vm_writer = VictoriaMetricsWriter()
    return _vm_writer


def _radacct_signals(
    db: Session, *, now: datetime, stale_after_seconds: int
) -> dict[str, int | float | None]:
    """Accounting-plane signals read from every configured external radacct DB.

    ``acct_freshness_seconds`` is the age of the NEWEST interim update across
    open sessions — if accounting stops flowing entirely, this climbs at
    wall-clock speed. ``radacct_read_ok`` is 0 when any configured source
    failed to answer (partial data must not masquerade as health).
    """
    from app.services.radius_session_reconcile import (
        _active_external_sync_configs,
        _get_external_engine,
        _radacct_table,
    )

    configs = _active_external_sync_configs(db)
    if not configs:
        return {
            "radacct_read_ok": 0,
            "open_sessions": 0,
            "stale_open_sessions": 0,
            "acct_freshness_seconds": None,
        }

    radacct = _radacct_table()
    open_sessions = 0
    stale_open = 0
    newest_update: datetime | None = None
    read_ok = 1
    for config in configs:
        try:
            engine = _get_external_engine(config["db_url"])
            with engine.connect() as conn:
                total, newest = conn.execute(
                    select(
                        func.count(),
                        func.max(
                            func.coalesce(
                                radacct.c.acctupdatetime, radacct.c.acctstarttime
                            )
                        ),
                    ).where(radacct.c.acctstoptime.is_(None))
                ).one()
                # Aware cutoff, same convention as the session reconcile's
                # radacct reads (FreeRADIUS pg schema uses timestamptz).
                stale_cutoff = now - timedelta(seconds=stale_after_seconds)
                stale = conn.execute(
                    select(func.count())
                    .where(radacct.c.acctstoptime.is_(None))
                    .where(
                        func.coalesce(radacct.c.acctupdatetime, radacct.c.acctstarttime)
                        < stale_cutoff
                    )
                ).scalar()
        except Exception:
            logger.exception(
                "radius_health_radacct_read_failed source=%s", config.get("name")
            )
            read_ok = 0
            continue
        open_sessions += int(total or 0)
        stale_open += int(stale or 0)
        if newest is not None:
            if newest.tzinfo is None:
                newest = newest.replace(tzinfo=UTC)
            if newest_update is None or newest > newest_update:
                newest_update = newest

    freshness: float | None = None
    if newest_update is not None:
        freshness = max(0.0, (now - newest_update).total_seconds())
    return {
        "radacct_read_ok": read_ok,
        "open_sessions": open_sessions,
        "stale_open_sessions": stale_open,
        "acct_freshness_seconds": freshness,
    }


def _enforcement_signals(db: Session) -> dict[str, int]:
    """Session-vs-billing drift, from the reconciled live-session view.

    ``suspended_with_session``: subscriptions the billing side suspended or
    blocked that still hold a live RADIUS session — enforcement is not
    landing on the NAS. ``paid_active_without_session``: ACTIVE
    subscriptions with a RADIUS login and no live session — a trend metric,
    not an alert (a powered-off router is legitimate).
    """
    from app.models.catalog import Subscription, SubscriptionStatus
    from app.models.radius_active_session import RadiusActiveSession

    active_sessions = int(
        db.execute(select(func.count()).select_from(RadiusActiveSession)).scalar() or 0
    )

    suspended_with_session = int(
        db.execute(
            select(func.count())
            .select_from(RadiusActiveSession)
            .join(
                Subscription,
                Subscription.id == RadiusActiveSession.subscription_id,
            )
            .where(
                Subscription.status.in_(
                    (SubscriptionStatus.suspended, SubscriptionStatus.blocked)
                )
            )
        ).scalar()
        or 0
    )

    session_logins = select(RadiusActiveSession.username)
    paid_without_session = int(
        db.execute(
            select(func.count())
            .select_from(Subscription)
            .where(
                Subscription.status == SubscriptionStatus.active,
                Subscription.login.isnot(None),
                Subscription.login != "",
                Subscription.login.not_in(session_logins),
            )
        ).scalar()
        or 0
    )

    return {
        "active_sessions": active_sessions,
        "suspended_with_session": suspended_with_session,
        "paid_active_without_session": paid_without_session,
    }


def collect_radius_health(
    db: Session,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = DEFAULT_STALE_SESSION_SECONDS,
) -> dict:
    """One health pass: radacct signals + enforcement drift, plain scalars."""
    now = now or datetime.now(UTC)
    health: dict = {"checked_at": now.isoformat()}
    health.update(
        _radacct_signals(db, now=now, stale_after_seconds=stale_after_seconds)
    )
    health.update(_enforcement_signals(db))
    try:
        from app.services.radius_probe import run_configured_probe

        probe_fields, _result = run_configured_probe()
        health.update(probe_fields)
    except Exception:  # the probe is additive; never fail the health pass
        logger.exception("radius_health_probe_failed")
        health.setdefault("probe_configured", 0)
    return health


def push_radius_metrics(health: dict, *, now: datetime | None = None) -> dict[str, int]:
    """Push the health counters to VictoriaMetrics as trend series."""
    ts_ms = int((now or datetime.now(UTC)).timestamp() * 1000)
    gauges = {
        "radius_open_sessions": health.get("open_sessions"),
        "radius_stale_open_sessions": health.get("stale_open_sessions"),
        "radius_acct_freshness_seconds": health.get("acct_freshness_seconds"),
        "radius_active_sessions": health.get("active_sessions"),
        "radius_suspended_with_active_session": health.get("suspended_with_session"),
        "radius_paid_active_without_session": health.get("paid_active_without_session"),
        "radius_radacct_read_ok": health.get("radacct_read_ok"),
        "radius_auth_rtt_ms": health.get("auth_rtt_ms"),
        "radius_probe_ok": (
            health.get("probe_ok") if health.get("probe_configured") else None
        ),
        "radius_probe_retries": (
            health.get("probe_retries") if health.get("probe_configured") else None
        ),
    }
    lines = [
        f"{name} {float(value)} {ts_ms}"
        for name, value in gauges.items()
        if value is not None
    ]
    if not lines:
        return {"radius_metric_lines": 0, "radius_metric_write_failed": 0}
    write_result = _writer().write_prometheus_lines(
        lines,
        adapter="radius.health",
        operation="radius_health",
    )
    return {
        "radius_metric_lines": len(lines),
        "radius_metric_write_failed": 0 if write_result.success else len(lines),
    }
