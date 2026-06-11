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

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Column, DateTime, String, func, select
from sqlalchemy.orm import Session

from app.models.catalog import AccessCredential, Subscription, SubscriptionStatus
from app.services.radius import (
    _active_external_sync_configs,
    _external_radius_table,
    _get_external_engine,
)

logger = logging.getLogger(__name__)

# A subscriber counts as "fully blocked" when they have at least one sub in
# a blocked status and no active sub. (Pending/hidden subs grant no access,
# so they don't lift the block.)
_BLOCKED_STATUSES = (
    SubscriptionStatus.suspended,
    SubscriptionStatus.blocked,
    SubscriptionStatus.stopped,
)

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
    """Active-credential usernames of subscribers with >=1 blocked sub and
    no active sub."""
    blocked_subscribers = (
        select(Subscription.subscriber_id)
        .where(Subscription.status.in_(_BLOCKED_STATUSES))
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

    Leak classes (each a list of usernames, capped at SAMPLE_LIMIT for the
    payload; counts are exact):

    - ``usable_password``: a password row exists in radcheck with no
      ``Auth-Type`` override — the subscriber can re-authenticate.
    - ``in_active_group``: radusergroup says ``dotmac-active`` — wrong group
      for a blocked subscriber (matters once group routing is enforcing).
    - ``open_session``: an open radacct session updated within the last
      2 hours — the subscriber is online right now.
    """
    usernames = _fully_blocked_usernames(db)
    result: dict[str, Any] = {
        "ok": True,
        "checked_usernames": len(usernames),
        "mixed_status_subscribers": mixed_status_subscriber_count(db),
        "usable_password": [],
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

    usable_password: set[str] = set()
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
                    usable_password |= password_users - rejected_users
                    in_active_group |= set(
                        conn.execute(
                            select(radusergroup.c.username)
                            .distinct()
                            .where(radusergroup.c.username.in_(chunk))
                            .where(radusergroup.c.groupname == "dotmac-active")
                        ).scalars()
                    )
                    open_session |= set(
                        conn.execute(
                            select(radacct.c.username)
                            .distinct()
                            .where(radacct.c.username.in_(chunk))
                            .where(radacct.c.acctstoptime.is_(None))
                            .where(radacct.c.acctupdatetime >= session_cutoff)
                        ).scalars()
                    )
        except Exception:
            logger.exception("Suspension audit failed against external RADIUS config.")
            result["errors"] += 1

    result["usable_password"] = sorted(usable_password)
    result["in_active_group"] = sorted(in_active_group)
    result["open_session"] = sorted(open_session)
    return _finalize(result)


def _finalize(result: dict[str, Any]) -> dict[str, Any]:
    counts = {
        kind: len(result[kind])
        for kind in ("usable_password", "in_active_group", "open_session")
    }
    result["counts"] = counts
    result["ok"] = result["errors"] == 0 and not any(counts.values())
    # Cap the username lists so the task result/log stays readable.
    for kind in counts:
        result[kind] = result[kind][:SAMPLE_LIMIT]

    if not result["ok"]:
        logger.warning(
            "Suspension enforcement audit found leaks: "
            "usable_password=%s in_active_group=%s open_session=%s "
            "(checked=%s, mixed_status_subscribers=%s, errors=%s). "
            "Samples: %s",
            counts["usable_password"],
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
