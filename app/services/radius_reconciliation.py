"""Suspension-enforcement reconciliation audit.

Asserts the invariant "every fully-blocked subscriber is actually
unreachable" against the external FreeRADIUS DB. Drift here accumulated
invisibly before (suspended subscribers with usable passwords, stale
``dotmac-active`` group rows, sessions surviving suspension), so the audit
reports each leak class explicitly instead of one rolled-up number.

Read-only: this module never mutates RADIUS state. Fixing a reported leak
is the enforcement/sync paths' job.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, Column, DateTime, String, func, select
from sqlalchemy.orm import Session

from app.models.catalog import AccessCredential, Subscription, SubscriptionStatus
from app.services.radius import (
    _active_external_sync_configs,
    _external_radius_table,
    _get_external_engine,
)
from app.services.radius_access_state import (
    BLOCKED_STATUSES as _BLOCKED_STATUSES,
)
from app.services.radius_access_state import (
    NO_ACCESS_STATUSES as _NO_ACCESS_STATUSES,
)

logger = logging.getLogger(__name__)

# Statuses that mean "should have no normal access" = walled-garden (blocked)
# OR removed (terminated). The audit targets this whole set so a `disabled`/
# `canceled`/`expired` subscriber that still has a usable password or live
# session is flagged too — previously the audit used only the blocked set and
# silently under-reported terminated leaks. ``_BLOCKED_STATUSES`` (imported
# from the canonical classifier) is kept for the mixed-status report below.

# Per-user walled-garden marker written by app.services.radius_population and
# the enforcement address-list path (MikroTik filter rules allow only the
# portal for IPs on this list).
WALLED_GARDEN_ADDRESS_LIST = "suspended"

# How fresh an open radacct session must be to count as "still online".
# Interim updates arrive every ~5 minutes; 2 hours tolerates missed interims
# without counting reaper-fodder ghosts.
_OPEN_SESSION_WINDOW = timedelta(hours=2)

_CHUNK = 500

SAMPLE_LIMIT = 20


def _chunked(values: list[str], size: int = _CHUNK):
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def _fully_blocked_usernames(db: Session) -> list[str]:
    """Active-credential usernames of subscribers with >=1 no-access sub
    (blocked or terminated) and no active sub."""
    blocked_subscribers = (
        select(Subscription.subscriber_id)
        .where(Subscription.status.in_(_NO_ACCESS_STATUSES))
        .distinct()
        .subquery()
    )
    active_subscribers = (
        select(Subscription.subscriber_id)
        .where(Subscription.status == SubscriptionStatus.active)
        .distinct()
        .subquery()
    )
    rows = db.execute(
        select(AccessCredential.username)
        .distinct()
        .where(AccessCredential.is_active.is_(True))
        .where(AccessCredential.subscriber_id.in_(select(blocked_subscribers)))
        .where(AccessCredential.subscriber_id.notin_(select(active_subscribers)))
    ).scalars()
    return [u for u in rows if u]


def mixed_status_subscriber_count(db: Session) -> int:
    """Subscribers with BOTH a blocked sub and an active sub. Their shared
    credential keeps full access (most-permissive aggregate), so per-service
    suspension does nothing for them — reported for visibility."""
    blocked_subscribers = (
        select(Subscription.subscriber_id)
        .where(Subscription.status.in_(_BLOCKED_STATUSES))
        .distinct()
        .subquery()
    )
    return int(
        db.execute(
            select(func.count(func.distinct(Subscription.subscriber_id)))
            .where(Subscription.status == SubscriptionStatus.active)
            .where(Subscription.subscriber_id.in_(select(blocked_subscribers)))
        ).scalar()
        or 0
    )


def audit_suspension_enforcement(db: Session) -> dict[str, Any]:
    """Check every fully-blocked subscriber against the external RADIUS DB.

    Enforcement model (captive-by-default): a blocked subscriber is
    INTENTIONALLY still able to authenticate when they carry a walled-garden
    marker — a ``Mikrotik-Address-List = suspended`` radreply row (today's
    per-user mechanism) or membership in ``dotmac-captive`` /
    ``dotmac-suspended`` groups (group routing). Hard reject = an
    ``Auth-Type`` radcheck override or the ``dotmac-suspended`` group.

    Leak classes (lists capped at SAMPLE_LIMIT in the payload; counts exact):

    - ``open_access``: password usable with NO reject override and NO
      walled-garden marker — the subscriber has unrestricted access.
    - ``in_active_group``: radusergroup says ``dotmac-active`` — wrong group
      for a blocked subscriber (matters once group routing is enforcing).
    - ``open_session``: an open radacct session updated within the last
      2 hours for a subscriber with no walled-garden marker — online with
      unrestricted access right now. (Captive subscribers online is by
      design — they can reach the pay page.)
    """
    usernames = _fully_blocked_usernames(db)
    result: dict[str, Any] = {
        "ok": True,
        "checked_usernames": len(usernames),
        "mixed_status_subscribers": mixed_status_subscriber_count(db),
        "open_access": [],
        "in_active_group": [],
        "open_session": [],
        "errors": 0,
    }
    if not usernames:
        return _finalize(result)

    configs = _active_external_sync_configs(db)
    if not configs:
        logger.warning(
            "Suspension audit: no external RADIUS config available — "
            "%s blocked usernames unverifiable.",
            len(usernames),
        )
        result["errors"] += 1
        return _finalize(result)

    open_access: set[str] = set()
    in_active_group: set[str] = set()
    open_session: set[str] = set()

    for config in configs:
        try:
            engine = _get_external_engine(config["db_url"])
            radcheck = _external_radius_table(
                config.get("radcheck_table", "radcheck"),
                Column("username", String),
                Column("attribute", String),
                Column("value", String),
            )
            radreply = _external_radius_table(
                config.get("radreply_table", "radreply"),
                Column("username", String),
                Column("attribute", String),
                Column("value", String),
            )
            radusergroup = _external_radius_table(
                config.get("radusergroup_table", "radusergroup"),
                Column("username", String),
                Column("groupname", String),
            )
            radacct = _external_radius_table(
                "radacct",
                Column("username", String),
                Column("acctstoptime", DateTime),
                Column("acctupdatetime", DateTime),
            )
            session_cutoff = datetime.now(UTC) - _OPEN_SESSION_WINDOW
            with engine.connect() as conn:
                for chunk in _chunked(usernames):
                    password_users = set(
                        conn.execute(
                            select(radcheck.c.username)
                            .distinct()
                            .where(radcheck.c.username.in_(chunk))
                            .where(radcheck.c.attribute.like("%Password%"))
                        ).scalars()
                    )
                    rejected_users = set(
                        conn.execute(
                            select(radcheck.c.username)
                            .distinct()
                            .where(radcheck.c.username.in_(chunk))
                            .where(radcheck.c.attribute == "Auth-Type")
                        ).scalars()
                    )
                    walled_users = set(
                        conn.execute(
                            select(radreply.c.username)
                            .distinct()
                            .where(radreply.c.username.in_(chunk))
                            .where(radreply.c.attribute == "Mikrotik-Address-List")
                            .where(radreply.c.value == WALLED_GARDEN_ADDRESS_LIST)
                        ).scalars()
                    ) | set(
                        conn.execute(
                            select(radusergroup.c.username)
                            .distinct()
                            .where(radusergroup.c.username.in_(chunk))
                            .where(
                                radusergroup.c.groupname.in_(
                                    ["dotmac-captive", "dotmac-suspended"]
                                )
                            )
                        ).scalars()
                    )
                    open_access |= password_users - rejected_users - walled_users
                    in_active_group |= set(
                        conn.execute(
                            select(radusergroup.c.username)
                            .distinct()
                            .where(radusergroup.c.username.in_(chunk))
                            .where(radusergroup.c.groupname == "dotmac-active")
                        ).scalars()
                    )
                    open_session |= (
                        set(
                            conn.execute(
                                select(radacct.c.username)
                                .distinct()
                                .where(radacct.c.username.in_(chunk))
                                .where(radacct.c.acctstoptime.is_(None))
                                .where(radacct.c.acctupdatetime >= session_cutoff)
                            ).scalars()
                        )
                        - walled_users
                    )
        except Exception:
            logger.exception("Suspension audit failed against external RADIUS config.")
            result["errors"] += 1

    result["open_access"] = sorted(open_access)
    result["in_active_group"] = sorted(in_active_group)
    result["open_session"] = sorted(open_session)
    return _finalize(result)


def _finalize(result: dict[str, Any]) -> dict[str, Any]:
    counts = {
        kind: len(result[kind])
        for kind in ("open_access", "in_active_group", "open_session")
    }
    result["counts"] = counts
    result["ok"] = result["errors"] == 0 and not any(counts.values())
    # Cap the username lists so the task result/log stays readable.
    for kind in counts:
        result[kind] = result[kind][:SAMPLE_LIMIT]

    if not result["ok"]:
        logger.warning(
            "Suspension enforcement audit found leaks: "
            "open_access=%s in_active_group=%s open_session=%s "
            "(checked=%s, mixed_status_subscribers=%s, errors=%s). "
            "Samples: %s",
            counts["open_access"],
            counts["in_active_group"],
            counts["open_session"],
            result["checked_usernames"],
            result["mixed_status_subscribers"],
            result["errors"],
            {k: result[k] for k in counts},
        )
    else:
        logger.info(
            "Suspension enforcement audit clean: %s blocked usernames "
            "verified (mixed_status_subscribers=%s).",
            result["checked_usernames"],
            result["mixed_status_subscribers"],
        )
    return result


# --- latest-result storage (Redis) -----------------------------------------
#
# The audit runs in a Celery worker, but only the web process serves
# /metrics — a Gauge set in the worker is never scraped (and prefork workers
# recycle). The task stores its result here; a custom collector registered in
# app.metrics reads it back at scrape time in the web process.

_AUDIT_RESULT_KEY = "radius:suspension_audit:latest"
_AUDIT_RESULT_TTL = int(timedelta(days=7).total_seconds())
_redis_client: Any = None


def _get_redis() -> Any:
    global _redis_client
    if _redis_client is None:
        url = os.getenv("REDIS_URL")
        if not url:
            return None
        import redis

        # Cached per process — never build clients per call (OOM lesson).
        _redis_client = redis.Redis.from_url(
            url, socket_timeout=2, socket_connect_timeout=2
        )
    return _redis_client


def store_latest_audit(result: dict[str, Any]) -> bool:
    """Persist the audit result for the web-process metrics collector."""
    client = _get_redis()
    if client is None:
        logger.warning("Suspension audit: REDIS_URL unset — result not stored.")
        return False
    payload = dict(result)
    payload["ran_at"] = datetime.now(UTC).isoformat()
    try:
        client.set(_AUDIT_RESULT_KEY, json.dumps(payload), ex=_AUDIT_RESULT_TTL)
        return True
    except Exception as exc:
        logger.warning("Suspension audit: failed to store result: %s", exc)
        return False


def load_latest_audit() -> dict[str, Any] | None:
    """Latest stored audit result, or None. Never raises (scrape path)."""
    try:
        client = _get_redis()
        if client is None:
            return None
        raw = client.get(_AUDIT_RESULT_KEY)
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        logger.debug("Suspension audit: load failed.", exc_info=True)
        return None


# Close open radacct rows whose accounting feed has gone silent. A live session
# advances acctupdatetime via interim accounting every Acct-Interim-Interval
# (300s), so "open but no update since the cutoff" reliably means dead (NAS
# down/rebooted, or a lost Acct-Stop). Nothing else ages radacct — the app-side
# RadiusAccountingSession reaper only closes the mirror — so without this, dead
# NAS leave phantom "online" sessions forever (inflating online/usage counts and
# wasting the enforcement reconciler's capped CoA budget on dead sessions).
_RADACCT_REAP_STALE_DEFAULT_SECONDS = 7200  # 2h (24x the 300s interim interval)
_RADACCT_REAP_STALE_FLOOR_SECONDS = 1800  # never reap anything fresher than 30m


def reap_stale_radacct_ghosts(
    db: Session,
    *,
    stale_after_seconds: int = _RADACCT_REAP_STALE_DEFAULT_SECONDS,
    batch: int = 5000,
) -> dict[str, int]:
    """Synthetic-close stale-open radacct sessions across external RADIUS DBs.

    acctstoptime is set to the last time we actually saw the session (not the
    reap time), which is closer to the truth for usage. Age-based only — no CoA
    dependency — so it converges even when the NAS is unreachable. Safe because
    interim accounting keeps genuinely-live sessions well under the cutoff.
    """
    cutoff_seconds = max(int(stale_after_seconds), _RADACCT_REAP_STALE_FLOOR_SECONDS)
    cutoff = datetime.now(UTC) - timedelta(seconds=cutoff_seconds)
    reaped = 0
    for config in _active_external_sync_configs(db):
        try:
            engine = _get_external_engine(config["db_url"])
            radacct = _external_radius_table(
                "radacct",
                Column("radacctid", BigInteger, primary_key=True),
                Column("acctstarttime", DateTime),
                Column("acctstoptime", DateTime),
                Column("acctupdatetime", DateTime),
                Column("acctterminatecause", String),
            )
            last_seen = func.coalesce(radacct.c.acctupdatetime, radacct.c.acctstarttime)
            with engine.begin() as conn:
                ids = [
                    row[0]
                    for row in conn.execute(
                        select(radacct.c.radacctid)
                        .where(radacct.c.acctstoptime.is_(None))
                        .where(last_seen < cutoff)
                        .limit(batch)
                    )
                ]
                if not ids:
                    continue
                conn.execute(
                    radacct.update()
                    .where(radacct.c.radacctid.in_(ids))
                    .values(
                        acctstoptime=last_seen,
                        acctterminatecause="Ghost-Reaped",
                    )
                )
                reaped += len(ids)
        except Exception:
            logger.warning("radacct ghost reap failed for a sync target", exc_info=True)
    if reaped:
        logger.info("radacct ghost reap closed %d stale sessions", reaped)
    return {"reaped": reaped}
