"""IPv4 consistency audit — step 1 of the connectivity-reconciler hardening.

Quantifies drift between the THREE places an active subscriber's IPv4 lives,
without touching any device (read-only). See
``docs/designs/SERVICE_LIFECYCLE_BUNDLE_INTEGRITY.md``.

The three sources, in sync when a subscriber has a pinned (static) IPv4:

  1. ``subscription.ipv4_address``      — the load-bearing column; what
     ``build_radius_reply_attributes`` writes into ``radreply`` as
     ``Framed-IP-Address``. The single point of failure flagged as risk R2:
     while suspended, the external row is deleted and this column is the ONLY
     copy of the address.
  2. The active ``IPAssignment`` (ipv4) → ``IPv4Address.address`` — the IPAM
     record, the only one with allocate/release (``is_active``) semantics.
  3. The external ``radreply`` ``Framed-IP-Address`` for the sub's login — what
     the NAS actually enforces.

Population: active subscriptions that are SUPPOSED to carry a pinned IPv4 —
i.e. at least one of the three sources is set. Purely-dynamic subs (none set)
are not drift and are excluded.

Drift classes (counts exact; sample lists capped):

  - ``assignment_missing``  — column IP set, but no active IPAssignment backs
    it. The core R2 metric: lose the column and there is nothing to recover.
  - ``assignment_mismatch`` — column IP and IPAssignment IP both set, disagree.
  - ``radreply_missing``    — column IP set and the login is provisioned in
    radcheck, but no Framed-IP in radreply. Customer not getting their IP.
  - ``radreply_mismatch``   — radreply Framed-IP and column IP both set,
    disagree. The NAS enforces a different IP than the system believes.
  - ``radreply_orphan``     — radreply pins a Framed-IP the system no longer
    tracks (column empty).

Read-only: never mutates app DB or RADIUS state.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Column, String, select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPVersion, IPv4Address
from app.services.radius import (
    _active_external_sync_configs,
    _external_radius_table,
    _get_external_engine,
)

logger = logging.getLogger(__name__)

_CHUNK = 500
SAMPLE_LIMIT = 20

_DRIFT_KINDS = (
    "assignment_missing",
    "assignment_mismatch",
    "radreply_missing",
    "radreply_mismatch",
    "radreply_orphan",
)


def _norm(ip: str | None) -> str:
    """Canonical string form for comparison, tolerant of junk."""
    if not ip:
        return ""
    text = str(ip).strip()
    if not text:
        return ""
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return text


def _chunked(values: list[str], size: int = _CHUNK):
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def _active_assignment_ips(db: Session) -> dict[str, str]:
    """Active ipv4 IPAssignment address keyed by subscriber_id.

    Keyed by subscriber, not subscription, on purpose: IPAssignment is
    subscriber-scoped, and prod's ``ip_assignments`` table has no
    ``subscription_id`` column (migration 153 stamped-not-applied — the alembic
    wedge documented in the design doc). Subscriber-level keying is both
    correct for the IPAM model and resilient to that drift."""
    by_subscriber: dict[str, str] = {}
    rows = db.execute(
        select(
            IPAssignment.subscriber_id,
            IPv4Address.address,
        )
        .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
        .where(IPAssignment.is_active.is_(True))
        .where(IPAssignment.ip_version == IPVersion.ipv4)
    ).all()
    for subscriber_id, address in rows:
        norm = _norm(address)
        if not norm:
            continue
        if subscriber_id is not None:
            by_subscriber.setdefault(str(subscriber_id), norm)
    return by_subscriber


def _external_ip_state(
    db: Session, logins: list[str]
) -> tuple[dict[str, str], set[str], int]:
    """For the given logins, read external RADIUS: a map login→Framed-IP and
    the set of logins present in radcheck (provisioned). Returns
    (framed_ip_by_login, provisioned_logins, errors)."""
    framed: dict[str, str] = {}
    provisioned: set[str] = set()
    errors = 0
    configs = _active_external_sync_configs(db)
    if not configs:
        logger.warning(
            "IP consistency audit: no external RADIUS config — %d logins "
            "unverifiable against radreply/radcheck.",
            len(logins),
        )
        return framed, provisioned, 1
    for config in configs:
        try:
            engine = _get_external_engine(config["db_url"])
            radreply = _external_radius_table(
                config.get("radreply_table", "radreply"),
                Column("username", String),
                Column("attribute", String),
                Column("value", String),
            )
            radcheck = _external_radius_table(
                config.get("radcheck_table", "radcheck"),
                Column("username", String),
                Column("attribute", String),
            )
            with engine.connect() as conn:
                for chunk in _chunked(logins):
                    for username, value in conn.execute(
                        select(radreply.c.username, radreply.c.value)
                        .where(radreply.c.username.in_(chunk))
                        .where(radreply.c.attribute == "Framed-IP-Address")
                    ).all():
                        norm = _norm(value)
                        if norm:
                            framed.setdefault(username, norm)
                    provisioned |= set(
                        conn.execute(
                            select(radcheck.c.username)
                            .distinct()
                            .where(radcheck.c.username.in_(chunk))
                        ).scalars()
                    )
        except Exception:
            logger.exception(
                "IP consistency audit failed against external RADIUS config."
            )
            errors += 1
    return framed, provisioned, errors


def audit_ip_consistency(db: Session) -> dict[str, Any]:
    """Compare the three IPv4 sources for every active subscription that is
    supposed to carry a pinned IPv4. Returns a result dict with exact counts
    and capped sample lists (login or subscription id per finding)."""
    result: dict[str, Any] = {
        "ok": True,
        "population": 0,
        "errors": 0,
    }
    for kind in _DRIFT_KINDS:
        result[kind] = []

    by_subscriber_assign = _active_assignment_ips(db)

    active = db.execute(
        select(
            Subscription.id,
            Subscription.login,
            Subscription.ipv4_address,
            Subscription.subscriber_id,
        ).where(Subscription.status == SubscriptionStatus.active)
    ).all()

    # First pass: resolve column + assignment IPs, decide who needs a radreply
    # lookup. A sub is in-population if any of the (so far two) sources is set;
    # radreply is added below.
    candidates: list[dict[str, Any]] = []
    logins: set[str] = set()
    for sub_id, login, col_ip_raw, subscriber_id in active:
        col_ip = _norm(col_ip_raw)
        assign_ip = (
            by_subscriber_assign.get(str(subscriber_id)) if subscriber_id else None
        ) or ""
        login = (login or "").strip()
        candidates.append(
            {
                "sub_id": str(sub_id),
                "login": login,
                "col_ip": col_ip,
                "assign_ip": assign_ip,
            }
        )
        if login:
            logins.add(login)

    framed_by_login, provisioned, ext_errors = _external_ip_state(db, sorted(logins))
    result["errors"] += ext_errors

    drift: dict[str, set[str]] = {kind: set() for kind in _DRIFT_KINDS}
    population = 0
    for c in candidates:
        col_ip = c["col_ip"]
        assign_ip = c["assign_ip"]
        login = c["login"]
        radreply_ip = framed_by_login.get(login, "") if login else ""

        if not (col_ip or assign_ip or radreply_ip):
            continue  # purely dynamic — not in scope
        population += 1
        tag = login or c["sub_id"]

        # Column vs IPAM
        if col_ip and not assign_ip:
            drift["assignment_missing"].add(tag)
        elif col_ip and assign_ip and col_ip != assign_ip:
            drift["assignment_mismatch"].add(tag)

        # Column vs external radreply (only meaningful when the login maps and
        # is actually provisioned — else radreply_missing would false-positive
        # on dynamic/unprovisioned logins).
        if login and login in provisioned:
            if col_ip and not radreply_ip:
                drift["radreply_missing"].add(tag)
            elif col_ip and radreply_ip and col_ip != radreply_ip:
                drift["radreply_mismatch"].add(tag)
            elif radreply_ip and not col_ip:
                drift["radreply_orphan"].add(tag)

    result["population"] = population
    for kind in _DRIFT_KINDS:
        result[kind] = sorted(drift[kind])
    return _finalize(result)


def _finalize(result: dict[str, Any]) -> dict[str, Any]:
    counts = {kind: len(result[kind]) for kind in _DRIFT_KINDS}
    result["counts"] = counts
    result["ok"] = result["errors"] == 0 and not any(counts.values())
    for kind in _DRIFT_KINDS:
        result[kind] = result[kind][:SAMPLE_LIMIT]

    if not result["ok"]:
        logger.warning(
            "IP consistency audit found drift (population=%s, errors=%s): %s. "
            "Samples: %s",
            result["population"],
            result["errors"],
            counts,
            {k: result[k] for k in _DRIFT_KINDS if result[k]},
        )
    else:
        logger.info(
            "IP consistency audit clean: %s pinned-IP active subs verified.",
            result["population"],
        )
    return result


# --- latest-result storage (Redis) -----------------------------------------
# Same pattern as radius_reconciliation: the task runs in a worker; the web
# process serves /metrics and reads the stored result at scrape time.

_AUDIT_RESULT_KEY = "radius:ip_consistency_audit:latest"
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


def store_latest_ip_audit(result: dict[str, Any]) -> bool:
    client = _get_redis()
    if client is None:
        logger.warning("IP consistency audit: REDIS_URL unset — result not stored.")
        return False
    payload = dict(result)
    payload["ran_at"] = datetime.now(UTC).isoformat()
    try:
        client.set(_AUDIT_RESULT_KEY, json.dumps(payload), ex=_AUDIT_RESULT_TTL)
        return True
    except Exception as exc:
        logger.warning("IP consistency audit: failed to store result: %s", exc)
        return False


def load_latest_ip_audit() -> dict[str, Any] | None:
    try:
        client = _get_redis()
        if client is None:
            return None
        raw = client.get(_AUDIT_RESULT_KEY)
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        logger.debug("IP consistency audit: load failed.", exc_info=True)
        return None
