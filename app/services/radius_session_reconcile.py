"""Discover-reconcile the live ``radius_active_sessions`` view from radacct.

The event-driven populator (``RadiusActiveSessionManager.on_acct_start/stop``,
wired to FreeRADIUS accounting hooks) is not firing in prod, so
``radius_active_sessions`` starved to a single row while the authoritative feed
— the external FreeRADIUS ``radacct`` table — carries ~893 genuinely-open
sessions, every one tagged with username, calling-station (router MAC),
framed IP and NAS IP.

This module reads the OPEN radacct sessions directly and upserts them into the
app-side ``radius_active_sessions`` table, then prunes rows whose session is no
longer open. Because it rediscovers the full open set on every run, it
self-heals regardless of whether the accounting hook ever fires — the same
discover-reconcile posture the ghost reaper already uses against radacct.

Read-only against the external RADIUS DB (SELECT only). Closing radacct rows is
solely the ghost reaper's job; this module never writes radacct.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    String,
    delete,
    or_,
    select,
)
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import OntAssignment, OntUnit
from app.models.radius_active_session import RadiusActiveSession
from app.services.radius import (
    _active_external_sync_configs,
    _external_radius_table,
    _get_external_engine,
)

logger = logging.getLogger(__name__)

# Single-flight guard shared by the task wrapper. "raS" = radius Active Session.
ADVISORY_LOCK_KEY = 0x72_61_53

# How fresh an open radacct session must be to count as "still online". Interim
# accounting advances acctupdatetime every ~5 min, so 15 min tolerates a couple
# missed interims while excluding dead-but-open ghosts (which the reaper closes
# at 2h). Overridable via the reconcile window setting so ops can widen it.
_DEFAULT_WINDOW_SECONDS = 900
_MIN_WINDOW_SECONDS = 300

# Postgres caps bound parameters at 65535; keep IN-list chunks well under it.
_CHUNK = 1000


def _release_postgres_read_transaction(db: Session) -> None:
    bind = db.get_bind()
    if bind.dialect.name.startswith("postgres"):
        db.rollback()


def _strip_inet_mask(value: object) -> str | None:
    """Normalize a radacct ``inet`` value to a bare host IP.

    ``radacct.nasipaddress`` / ``framedipaddress`` are Postgres ``inet``
    columns whose text form carries a netmask (e.g. ``160.119.127.95/32``),
    but ``nas_devices`` and ``subscriptions`` store bare IPs
    (``160.119.127.95``). Without stripping the ``/mask`` the NAS lookup and
    any downstream framed-IP match silently miss (this left nas_device_id NULL
    on every session). IPv6 hosts keep their form (the ``:`` is meaningful).

    psycopg adapts ``inet`` to ``ipaddress`` objects (``IPv4Address`` /
    ``IPv4Interface``), NOT strings, so ``str(value)`` first — it normalizes
    objects and raw string exports uniformly.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    host = text.split("/", 1)[0]
    # IPv6 addresses keep their full form; IPv4 host masks (/32) are dropped.
    return (text if ":" in host else host) or None


def _chunked(values: list[str], size: int = _CHUNK):
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def _radacct_table():
    """External radacct projection — only the columns the live view needs.

    ``framedipv6prefix`` / ``nasportid`` are included because real FreeRADIUS
    schemas carry them; a deployment lacking them would raise on SELECT, but the
    canonical FreeRADIUS ``radacct`` always has them.
    """
    return _external_radius_table(
        "radacct",
        Column("username", String),
        Column("acctsessionid", String),
        Column("callingstationid", String),
        Column("framedipaddress", String),
        Column("framedipv6prefix", String),
        Column("nasipaddress", String),
        Column("nasportid", String),
        Column("acctinputoctets", BigInteger),
        Column("acctoutputoctets", BigInteger),
        Column("acctstarttime", DateTime),
        Column("acctstoptime", DateTime),
        Column("acctupdatetime", DateTime),
    )


def _read_open_sessions(
    db: Session, cutoff: datetime, result: dict[str, int]
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Read OPEN radacct sessions across every configured external RADIUS DB.

    Returns ``(sessions_by_sid, complete)`` where ``complete`` is True only when
    every configured DB was read without error — the caller must NOT prune the
    live view on an incomplete read, or a transient radius-DB outage would wipe
    every session sourced from it.
    """
    configs = _active_external_sync_configs(db)
    if not configs:
        logger.warning("active-session reconcile: no external RADIUS config available")
        result["errors"] += 1
        return {}, False
    _release_postgres_read_transaction(db)

    by_sid: dict[str, dict[str, Any]] = {}
    complete = True
    radacct = _radacct_table()
    for config in configs:
        try:
            engine = _get_external_engine(config["db_url"])
            with engine.connect() as conn:
                rows = conn.execute(
                    select(
                        radacct.c.username,
                        radacct.c.acctsessionid,
                        radacct.c.callingstationid,
                        radacct.c.framedipaddress,
                        radacct.c.framedipv6prefix,
                        radacct.c.nasipaddress,
                        radacct.c.nasportid,
                        radacct.c.acctinputoctets,
                        radacct.c.acctoutputoctets,
                        radacct.c.acctstarttime,
                        radacct.c.acctupdatetime,
                    )
                    .where(radacct.c.acctstoptime.is_(None))
                    .where(radacct.c.acctupdatetime >= cutoff)
                ).all()
            for row in rows:
                sid = (row.acctsessionid or "").strip()
                if not sid:
                    result["skipped"] += 1
                    continue
                fresh = row.acctupdatetime or row.acctstarttime
                prev = by_sid.get(sid)
                if prev is not None:
                    prev_fresh = prev["acctupdatetime"] or prev["acctstarttime"]
                    if (
                        fresh is not None
                        and prev_fresh is not None
                        and fresh <= prev_fresh
                    ):
                        continue
                by_sid[sid] = {
                    "username": (row.username or "").strip() or None,
                    "acct_session_id": sid,
                    "calling_station_id": row.callingstationid,
                    "framed_ip_address": _strip_inet_mask(row.framedipaddress),
                    "framed_ipv6_prefix": row.framedipv6prefix,
                    "nas_ip_address": _strip_inet_mask(row.nasipaddress),
                    "nas_port_id": row.nasportid,
                    "bytes_in": int(row.acctinputoctets or 0),
                    "bytes_out": int(row.acctoutputoctets or 0),
                    "acctstarttime": row.acctstarttime,
                    "acctupdatetime": row.acctupdatetime,
                }
        except Exception:
            logger.warning(
                "active-session reconcile: read failed for a RADIUS target",
                exc_info=True,
            )
            result["errors"] += 1
            complete = False
    return by_sid, complete


def _resolve_active_subs(
    db: Session, usernames: set[str]
) -> dict[str, tuple[Any, Any]]:
    """Map radacct username -> (subscriber_id, subscription_id) via the
    ACTIVE subscription whose ``login`` equals the username.

    Duplicate-login dedupe: the subscriptions table has known duplicate rows, so
    a login can carry >1 ACTIVE subscription. We pick DETERMINISTICALLY by the
    lowest subscription id (ordered scan, first-wins) — there is no canonical
    login->subscription helper in the codebase (radius_population's by_login map
    is order-dependent), so lowest-id is the stable choice.
    """
    out: dict[str, tuple[Any, Any]] = {}
    names = [u for u in usernames if u]
    for chunk in _chunked(names):
        rows = db.execute(
            select(
                Subscription.login,
                Subscription.subscriber_id,
                Subscription.id,
            )
            .where(Subscription.login.in_(chunk))
            .where(Subscription.status == SubscriptionStatus.active)
            .order_by(Subscription.login, Subscription.id)
        ).all()
        for login, subscriber_id, subscription_id in rows:
            if login not in out:  # first per login == lowest id
                out[login] = (subscriber_id, subscription_id)
    return out


def _resolve_nas(db: Session, nas_ips: set[str]) -> dict[str, Any]:
    """Map radacct nasipaddress -> nas_devices.id, mirroring
    ``enforcement._nas_device_by_ip`` (match nas_ip / management_ip /
    ip_address, active only). Unresolved IPs are simply absent from the map so
    the caller leaves nas_device_id NULL rather than dropping the session."""
    from sqlalchemy import or_

    out: dict[str, Any] = {}
    ips = [ip for ip in nas_ips if ip]
    for chunk in _chunked(ips):
        rows = db.execute(
            select(
                NasDevice.id,
                NasDevice.nas_ip,
                NasDevice.management_ip,
                NasDevice.ip_address,
            )
            .where(NasDevice.is_active.is_(True))
            .where(
                or_(
                    NasDevice.nas_ip.in_(chunk),
                    NasDevice.management_ip.in_(chunk),
                    NasDevice.ip_address.in_(chunk),
                )
            )
            .order_by(NasDevice.id)
        ).all()
        wanted = set(chunk)
        for device_id, nas_ip, management_ip, ip_address in rows:
            for ip in (nas_ip, management_ip, ip_address):
                if ip in wanted and ip not in out:  # lowest id wins per IP
                    out[ip] = device_id
    return out


def _update_ont_runtime_from_radius(
    db: Session,
    *,
    username: str,
    subscriber_id: Any,
    framed_ip_address: str | None,
    observed_at: datetime,
) -> int:
    """Project the authoritative PPPoE framed IP onto assigned ONTs.

    TR-069 can report the ONT management/TR-069 address as a WAN IP on some
    devices. For active PPPoE subscribers, the RADIUS framed IP is the customer
    WAN address we want to show in app runtime fields.
    """
    if not framed_ip_address or subscriber_id is None:
        return 0

    assignments = db.execute(
        select(OntAssignment, OntUnit)
        .join(OntUnit, OntUnit.id == OntAssignment.ont_unit_id)
        .where(OntAssignment.active.is_(True))
        .where(OntAssignment.subscriber_id == subscriber_id)
        .where(
            or_(
                OntAssignment.pppoe_username == username,
                OntAssignment.pppoe_username.is_(None),
            )
        )
        .order_by(OntAssignment.assigned_at.desc(), OntAssignment.created_at.desc())
    ).all()

    changed = 0
    for _assignment, ont in assignments:
        before = (
            ont.observed_wan_ip,
            ont.observed_pppoe_status,
            ont.observed_runtime_updated_at,
        )
        ont.observed_wan_ip = framed_ip_address
        ont.observed_pppoe_status = "Connected"
        ont.observed_runtime_updated_at = observed_at
        after = (
            ont.observed_wan_ip,
            ont.observed_pppoe_status,
            ont.observed_runtime_updated_at,
        )
        if after != before:
            changed += 1
    return changed


def reconcile_active_sessions_from_radacct(
    db: Session, *, window_seconds: int | None = None
) -> dict[str, int]:
    """Rebuild ``radius_active_sessions`` from OPEN external radacct sessions.

    Upserts every open radacct session (keyed by acct_session_id) with its
    resolved subscriber/subscription (username->login) and NAS device
    (nasipaddress->nas_devices), then prunes rows whose session is no longer in
    the open set. Read-only against the external radius DB.
    """
    window = timedelta(
        seconds=max(int(window_seconds or _DEFAULT_WINDOW_SECONDS), _MIN_WINDOW_SECONDS)
    )
    run_start = datetime.now(UTC)
    cutoff = run_start - window
    result: dict[str, int] = {
        "seen_open": 0,
        "upserted_new": 0,
        "upserted_updated": 0,
        "pruned": 0,
        "unmatched_username": 0,
        "unresolved_nas": 0,
        "ont_runtime_updated": 0,
        "skipped": 0,
        "errors": 0,
    }

    by_sid, complete = _read_open_sessions(db, cutoff, result)
    result["seen_open"] = len(by_sid)
    if not by_sid and not complete:
        # Nothing readable this run — leave the live view untouched.
        return result

    usernames = {s["username"] for s in by_sid.values() if s["username"]}
    nas_ips = {s["nas_ip_address"] for s in by_sid.values() if s["nas_ip_address"]}
    sub_map = _resolve_active_subs(db, usernames)
    nas_map = _resolve_nas(db, nas_ips)

    # Existing rows for the open set, in one chunked lookup (not per-row).
    existing: dict[str, RadiusActiveSession] = {}
    sids = list(by_sid.keys())
    for chunk in _chunked(sids):
        for existing_row in db.scalars(
            select(RadiusActiveSession).where(
                RadiusActiveSession.acct_session_id.in_(chunk)
            )
        ).all():
            existing.setdefault(existing_row.acct_session_id, existing_row)

    now = datetime.now(UTC)
    for sid, s in by_sid.items():
        username = s["username"]
        resolved = sub_map.get(username) if username else None
        if resolved is None:
            # No ACTIVE subscription owns this login — count it, create no row.
            result["unmatched_username"] += 1
            continue
        subscriber_id, subscription_id = resolved

        nas_device_id = nas_map.get(s["nas_ip_address"])
        if s["nas_ip_address"] and nas_device_id is None:
            result["unresolved_nas"] += 1

        row: RadiusActiveSession | None = existing.get(sid)
        if row is None:
            db.add(
                RadiusActiveSession(
                    username=username,
                    acct_session_id=sid,
                    subscriber_id=subscriber_id,
                    subscription_id=subscription_id,
                    nas_device_id=nas_device_id,
                    nas_ip_address=s["nas_ip_address"],
                    framed_ip_address=s["framed_ip_address"],
                    framed_ipv6_prefix=s["framed_ipv6_prefix"],
                    calling_station_id=s["calling_station_id"],
                    nas_port_id=s["nas_port_id"],
                    bytes_in=s["bytes_in"],
                    bytes_out=s["bytes_out"],
                    session_start=s["acctstarttime"] or now,
                    last_update=now,
                )
            )
            result["upserted_new"] += 1
        else:
            row.username = username
            row.subscriber_id = subscriber_id
            row.subscription_id = subscription_id
            if nas_device_id is not None:
                row.nas_device_id = nas_device_id
            row.nas_ip_address = s["nas_ip_address"]
            row.framed_ip_address = s["framed_ip_address"]
            row.framed_ipv6_prefix = s["framed_ipv6_prefix"]
            row.calling_station_id = s["calling_station_id"]
            row.nas_port_id = s["nas_port_id"]
            row.bytes_in = s["bytes_in"]
            row.bytes_out = s["bytes_out"]
            row.last_update = now
            result["upserted_updated"] += 1
        result["ont_runtime_updated"] += _update_ont_runtime_from_radius(
            db,
            username=username,
            subscriber_id=subscriber_id,
            framed_ip_address=s["framed_ip_address"],
            observed_at=s["acctupdatetime"] or now,
        )

    db.flush()

    # Prune sessions that are no longer open — but only on a COMPLETE read, so a
    # transient radius-DB failure can't wipe the live view. Ended sessions are
    # pruned immediately; the only rows spared are those inserted AFTER this
    # run's radacct snapshot was taken (created_at >= run_start) — i.e. a
    # session that started concurrently with our read and so couldn't appear in
    # the snapshot. Those are re-affirmed on the next run.
    if complete:
        seen = set(by_sid.keys())
        current = [
            r[0] for r in db.execute(select(RadiusActiveSession.acct_session_id)).all()
        ]
        stale = [sid for sid in current if sid not in seen]
        for chunk in _chunked(stale):
            res = db.execute(
                delete(RadiusActiveSession)
                .where(RadiusActiveSession.acct_session_id.in_(chunk))
                .where(RadiusActiveSession.created_at < run_start)
            )
            result["pruned"] += res.rowcount or 0
        db.flush()

    logger.info("active-session reconcile: %s", result)
    return result
