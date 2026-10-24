"""IPv4 consistency audit — step 1 of the connectivity-reconciler hardening.

Quantifies drift between the THREE places an active subscriber's IPv4 lives,
without touching any device (read-only). See
``docs/FINANCIAL_ACCESS_ENFORCEMENT.md``.

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

Collision classes (tactical stopgap, pre-Starter dotmac-ipam cut; these are
cross-service, not per-subscription, so they sit outside the population):

  - ``ip_on_multiple_subscriptions`` — one ``ipv4_address`` on more than one
    RADIUS-servable subscription belonging to DIFFERENT subscribers (a real
    cross-customer collision). "Servable" is the canonical projection rule: a
    subscription whose login projection (``plan_login_radius_projections``)
    selects it and has ``plan.write_radreply`` (active, or blocked-but-captive).
    Rejected (disabled/stopped/...), terminal, hidden, pending and archived
    subscriptions never receive a Framed-IP-Address so cannot collide.
  - ``ip_shared_within_subscriber`` — informational: the same servable IP on
    several subscriptions of ONE subscriber (add-on/secondary service).
  - ``ip_column_reuse_historic`` — informational: the old broad count (same
    column value on >1 non-canceled/expired subscription, any status). Kept so
    the data is visible; never alerts and never flips ``ok``.
  - ``assignment_on_terminal_subscription`` — an active IPv4 IPAssignment whose
    subscription is canceled/expired (the address is held but not served).
  - ``session_ip_ipam_mismatch`` — an online subscription whose live framed IP
    is held (active IPAM assignment on a non-terminal subscription, or the
    column of a servable subscription) by a DIFFERENT subscriber's
    subscription. Real collision, critical.
  - ``session_ip_ipam_stale`` — an online subscription whose live framed IP
    differs from its own active IPAM assignment but is not held by anyone
    else (or only by another subscription of the same subscriber). IPAM
    hygiene, warning.
  - ``session_ip_duplicate`` — one framed IP on more than one live session.
  - ``session_ip_pool_not_linked`` — a live framed IP whose IPAM pool is not
    linked to the session's NAS (``radius_pool:`` tag or legacy FK).

Read-only: never mutates app DB or RADIUS state.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Column, String, func, select
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IpPool, IPv4Address, IPVersion
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.services.nas._helpers import radius_pool_ids_from_tags
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
# Cross-service collision classes; the first four are hard collisions, the
# pool/NAS link check is configuration-dependent (see the alert rules).
_COLLISION_KINDS = (
    "ip_on_multiple_subscriptions",
    "ip_shared_within_subscriber",
    "ip_column_reuse_historic",
    "assignment_on_terminal_subscription",
    "session_ip_ipam_mismatch",
    "session_ip_ipam_stale",
    "session_ip_duplicate",
    "session_ip_pool_not_linked",
)
# Alert severity per collision kind; mirrored by
# deploy/observability/ip_consistency.rules.yml (see the architecture test).
CRITICAL_KINDS = (
    "ip_on_multiple_subscriptions",
    "session_ip_ipam_mismatch",
    "session_ip_duplicate",
)
WARNING_KINDS = (
    "assignment_on_terminal_subscription",
    "session_ip_ipam_stale",
    "session_ip_pool_not_linked",
)
# Visible in counts but neither alerting nor affecting ``ok``.
INFORMATIONAL_KINDS = (
    "ip_shared_within_subscriber",
    "ip_column_reuse_historic",
)
_ALL_KINDS = _DRIFT_KINDS + _COLLISION_KINDS
_TERMINAL_STATUSES = (SubscriptionStatus.canceled, SubscriptionStatus.expired)


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
    """One unambiguous active IPv4 assignment keyed by exact subscription.

    Legacy subscriber-level inference is intentionally excluded. A missing
    exact bridge or multiple exact assignments is drift to adjudicate, not a
    reason to select an arbitrary sibling-service address.
    """
    candidates: dict[str, list[str]] = {}
    rows = db.execute(
        select(
            IPAssignment.subscription_id,
            IPv4Address.address,
        )
        .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
        .where(IPAssignment.is_active.is_(True))
        .where(IPAssignment.ip_version == IPVersion.ipv4)
        .where(IPAssignment.subscription_id.is_not(None))
    ).all()
    for subscription_id, address in rows:
        norm = _norm(address)
        if not norm or subscription_id is None:
            continue
        candidates.setdefault(str(subscription_id), []).append(norm)
    return {
        subscription_id: values[0]
        for subscription_id, values in candidates.items()
        if len(values) == 1
    }


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


def _servable_subscriptions(db: Session) -> list[Subscription]:
    """Subscriptions with a column IPv4 that RADIUS would serve a Framed-IP to.

    Uses the canonical projection planner (the same decision the RADIUS writer
    and the drift comparator consume): the login's selected subscription with
    ``plan.write_radreply`` (mode active/captive). Peers sharing a login are
    included in the planning pass so the per-login owner selection is exact.
    """
    from app.services.radius_access_state import ACTIVE_STATUSES, BLOCKED_STATUSES
    from app.services.radius_projection_planner import plan_login_radius_projections

    statuses = ACTIVE_STATUSES | BLOCKED_STATUSES
    with_ip = (
        db.execute(
            select(Subscription)
            .options(
                joinedload(Subscription.subscriber).joinedload(Subscriber.reseller)
            )
            .where(Subscription.status.in_(statuses))
            .where(Subscription.ipv4_address.is_not(None))
            .where(Subscription.ipv4_address != "")
            .where(Subscription.login.is_not(None))
        )
        .unique()
        .scalars()
        .all()
    )
    with_ip = [s for s in with_ip if (s.login or "").strip() and _norm(s.ipv4_address)]
    by_id = {s.id: s for s in with_ip}
    logins = sorted({(s.login or "").strip() for s in with_ip})
    for chunk in _chunked(logins):
        for peer in (
            db.execute(
                select(Subscription)
                .options(
                    joinedload(Subscription.subscriber).joinedload(Subscriber.reseller)
                )
                .where(Subscription.status.in_(statuses))
                .where(Subscription.login.in_(chunk))
            )
            .unique()
            .scalars()
            .all()
        ):
            by_id.setdefault(peer.id, peer)
    projections = plan_login_radius_projections(db, list(by_id.values()))
    servable: list[Subscription] = []
    for sub in with_ip:
        projection = projections.get((sub.login or "").strip())
        if (
            projection is not None
            and projection.subscription_id == str(sub.id)
            and projection.plan.write_radreply
        ):
            servable.append(sub)
    return servable


def _subscriber_numbers(db: Session, subscriber_ids: set[Any]) -> dict[str, str]:
    if not subscriber_ids:
        return {}
    return {
        str(sid): (number or "")
        for sid, number in db.execute(
            select(Subscriber.id, Subscriber.subscriber_number).where(
                Subscriber.id.in_(subscriber_ids)
            )
        ).all()
    }


def _collision_findings(
    db: Session, by_subscription_assign: dict[str, str]
) -> tuple[dict[str, set[str]], dict[str, dict[str, list[str]]]]:
    """Cross-service IPv4 collisions (read-only).

    Returns (findings, details). Findings samples are IPs; details maps
    kind -> ip -> sorted subscriber numbers (no other identifiers).
    """
    found: dict[str, set[str]] = {kind: set() for kind in _COLLISION_KINDS}
    holder_ids: dict[str, dict[str, set[Any]]] = {}  # kind -> ip -> subscriber ids

    def _note(kind: str, ip: str, *subscriber_ids: Any) -> None:
        found[kind].add(ip)
        holder_ids.setdefault(kind, {}).setdefault(ip, set()).update(
            sid for sid in subscriber_ids if sid is not None
        )

    # Historic broad count: same IPv4 on >1 non-terminal subscription of any
    # status. Informational only.
    shared = db.execute(
        select(Subscription.ipv4_address)
        .where(Subscription.ipv4_address.is_not(None))
        .where(Subscription.ipv4_address != "")
        .where(Subscription.status.notin_(_TERMINAL_STATUSES))
        .group_by(Subscription.ipv4_address)
        .having(func.count(Subscription.id) > 1)
    ).scalars()
    found["ip_column_reuse_historic"] = {_norm(v) for v in shared if _norm(v)}

    # Real collisions: only RADIUS-servable subscriptions.
    servable = _servable_subscriptions(db)
    servable_by_ip: dict[str, list[Subscription]] = {}
    for sub in servable:
        servable_by_ip.setdefault(_norm(sub.ipv4_address), []).append(sub)
    for ip, subs in servable_by_ip.items():
        if len(subs) < 2:
            continue
        subscriber_ids = {s.subscriber_id for s in subs}
        if len(subscriber_ids) > 1:
            _note("ip_on_multiple_subscriptions", ip, *subscriber_ids)
        else:
            _note("ip_shared_within_subscriber", ip, *subscriber_ids)

    # Active IPAM assignment on a terminal subscription.
    for address, in_sub in db.execute(
        select(IPv4Address.address, Subscription.id)
        .join(IPAssignment, IPAssignment.ipv4_address_id == IPv4Address.id)
        .join(Subscription, IPAssignment.subscription_id == Subscription.id)
        .where(IPAssignment.is_active.is_(True))
        .where(IPAssignment.ip_version == IPVersion.ipv4)
        .where(Subscription.status.in_(_TERMINAL_STATUSES))
    ).all():
        found["assignment_on_terminal_subscription"].add(_norm(address) or str(in_sub))

    # ip -> {subscription_id: subscriber_id} for everything that legitimately
    # holds the address: live IPAM assignments and servable column values.
    holders: dict[str, dict[str, Any]] = {}
    for address, sub_id, subscriber_id in db.execute(
        select(IPv4Address.address, Subscription.id, Subscription.subscriber_id)
        .join(IPAssignment, IPAssignment.ipv4_address_id == IPv4Address.id)
        .join(Subscription, IPAssignment.subscription_id == Subscription.id)
        .where(IPAssignment.is_active.is_(True))
        .where(IPAssignment.ip_version == IPVersion.ipv4)
        .where(Subscription.status.notin_(_TERMINAL_STATUSES))
    ).all():
        if _norm(address):
            holders.setdefault(_norm(address), {})[str(sub_id)] = subscriber_id
    for ip, subs in servable_by_ip.items():
        for sub in subs:
            holders.setdefault(ip, {})[str(sub.id)] = sub.subscriber_id

    sessions = db.execute(
        select(
            RadiusActiveSession.subscription_id,
            RadiusActiveSession.subscriber_id,
            RadiusActiveSession.framed_ip_address,
            RadiusActiveSession.nas_device_id,
        ).where(RadiusActiveSession.framed_ip_address.is_not(None))
    ).all()
    ip_counts: dict[str, int] = {}
    session_ips: list[tuple[str | None, str, Any]] = []
    session_subscriber: dict[str, Any] = {}
    for sub_id, session_subscriber_id, framed, nas_id in sessions:
        ip = _norm(framed)
        if not ip:
            continue
        ip_counts[ip] = ip_counts.get(ip, 0) + 1
        session_ips.append((str(sub_id) if sub_id else None, ip, nas_id))
        if sub_id:
            session_subscriber[str(sub_id)] = session_subscriber_id
    found["session_ip_duplicate"] = {ip for ip, n in ip_counts.items() if n > 1}
    for ip in found["session_ip_duplicate"]:
        holder_ids.setdefault("session_ip_duplicate", {}).setdefault(ip, set())

    # Live framed IP vs the subscription's active IPAM assignment. If another
    # SUBSCRIBER's subscription holds the address it is a real collision;
    # otherwise the assignment is merely stale (hygiene).
    for sub_id, ip, _nas in session_ips:
        assigned = by_subscription_assign.get(sub_id or "", "")
        if not assigned or assigned == ip:
            continue
        own_subscriber = session_subscriber.get(sub_id or "")
        foreign = {
            sid
            for other_sub, sid in holders.get(ip, {}).items()
            if other_sub != sub_id and sid != own_subscriber
        }
        if foreign:
            _note("session_ip_ipam_mismatch", ip, own_subscriber, *foreign)
        else:
            _note("session_ip_ipam_stale", ip, own_subscriber)

    # Framed IP whose pool is not linked to the session's NAS.
    if session_ips:
        pool_by_ip = {
            _norm(address): (pool_id, pool_nas_id)
            for address, pool_id, pool_nas_id in db.execute(
                select(IPv4Address.address, IpPool.id, IpPool.nas_device_id)
                .join(IpPool, IPv4Address.pool_id == IpPool.id)
                .where(IPv4Address.address.in_({ip for _, ip, _ in session_ips}))
            ).all()
        }
        nas_ids = {n for _, _, n in session_ips if n is not None}
        linked_by_nas = {
            nas.id: set(radius_pool_ids_from_tags(nas.tags))
            for nas in db.scalars(
                select(NasDevice).where(NasDevice.id.in_(nas_ids))
            ).all()
        }
        for _sub, ip, nas_id in session_ips:
            pool = pool_by_ip.get(ip)
            if pool is None or nas_id not in linked_by_nas:
                continue  # IP outside IPAM pools / unknown NAS: not judgeable
            pool_id, pool_nas_id = pool
            if str(pool_id) not in linked_by_nas[nas_id] and pool_nas_id != nas_id:
                found["session_ip_pool_not_linked"].add(ip)

    all_ids = {
        sid for by_ip in holder_ids.values() for ids in by_ip.values() for sid in ids
    }
    numbers = _subscriber_numbers(db, all_ids)
    details: dict[str, dict[str, list[str]]] = {}
    for kind, by_ip in holder_ids.items():
        details[kind] = {
            ip: sorted({numbers.get(str(sid)) or "-" for sid in ids})
            for ip, ids in sorted(by_ip.items())[:SAMPLE_LIMIT]
        }
    return found, details


def audit_ip_consistency(db: Session) -> dict[str, Any]:
    """Compare the three IPv4 sources for every active subscription that is
    supposed to carry a pinned IPv4. Returns a result dict with exact counts
    and capped sample lists (login or subscription id per finding)."""
    result: dict[str, Any] = {
        "ok": True,
        "population": 0,
        "errors": 0,
    }
    for kind in _ALL_KINDS:
        result[kind] = []

    by_subscription_assign = _active_assignment_ips(db)

    active_subscriptions = (
        db.execute(
            select(Subscription)
            .options(
                joinedload(Subscription.subscriber).joinedload(Subscriber.reseller)
            )
            .where(Subscription.status == SubscriptionStatus.active)
        )
        .unique()
        .scalars()
        .all()
    )
    from app.services.radius_projection_planner import plan_login_radius_projections

    desired_radius = plan_login_radius_projections(db, active_subscriptions)

    # First pass: resolve column + assignment IPs, decide who needs a radreply
    # lookup. A sub is in-population if any of the (so far two) sources is set;
    # radreply is added below.
    candidates: list[dict[str, Any]] = []
    logins: set[str] = set()
    for subscription in active_subscriptions:
        sub_id = subscription.id
        col_ip = _norm(subscription.ipv4_address)
        assign_ip = by_subscription_assign.get(str(sub_id), "")
        login = (subscription.login or "").strip()
        radius_projection = desired_radius.get(login) if login else None
        candidates.append(
            {
                "sub_id": str(sub_id),
                "login": login,
                "col_ip": col_ip,
                "assign_ip": assign_ip,
                "write_radreply": bool(
                    radius_projection is not None
                    and radius_projection.subscription_id == str(sub_id)
                    and radius_projection.plan.write_radreply
                ),
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
        if c["write_radreply"] and login and login in provisioned:
            if col_ip and not radreply_ip:
                drift["radreply_missing"].add(tag)
            elif col_ip and radreply_ip and col_ip != radreply_ip:
                drift["radreply_mismatch"].add(tag)
            elif radreply_ip and not col_ip:
                drift["radreply_orphan"].add(tag)

    result["population"] = population
    collisions, details = _collision_findings(db, by_subscription_assign)
    drift.update(collisions)
    result["sample_subscribers"] = details
    for kind in _ALL_KINDS:
        result[kind] = sorted(drift[kind])
    return _finalize(result)


def _finalize(result: dict[str, Any]) -> dict[str, Any]:
    counts = {kind: len(result[kind]) for kind in _ALL_KINDS}
    result["counts"] = counts
    result["ok"] = result["errors"] == 0 and not any(
        count for kind, count in counts.items() if kind not in INFORMATIONAL_KINDS
    )
    for kind in _ALL_KINDS:
        result[kind] = result[kind][:SAMPLE_LIMIT]

    if not result["ok"]:
        logger.warning(
            "IP consistency audit found drift (population=%s, errors=%s): %s. "
            "Samples: %s",
            result["population"],
            result["errors"],
            counts,
            {k: result[k] for k in _ALL_KINDS if result[k]},
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
