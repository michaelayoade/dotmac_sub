"""Pre-change backup of a subscriber's connectivity state — capture + restore.

Snapshots the external RADIUS rows (``radcheck``/``radreply``), the internal
credential/radius-user active flags, and the IP state (served columns + active
``IPAssignment`` rows) for one subscriber *before* a destructive connectivity
mutation. The reconciler/enforcement paths can then mutate with a way back.

Design rules:
- **Capture never breaks the mutation it guards.** ``capture_connectivity_state``
  swallows its own errors and returns ``None`` on failure (the caller also wraps
  it). A missing RADIUS DB degrades to a partial backup, not an exception.
- **It is a backup, not a source of truth.** The reconciler never reads these
  rows to make decisions; only an explicit ``restore`` consumes them.
- **Restore defaults to dry-run.** Applying a backup re-materializes the captured
  RADIUS rows and flips the local flags/IP back — an operator action, gated.

The RADIUS rows are read/written through the one canonical DSN
(``radius_dsn_libpq``) shared by both writers, so a restore can't split-brain.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.catalog import AccessCredential, Subscription
from app.models.connectivity_backup import ConnectivityStateBackup
from app.models.network import (
    IPAssignment,
    IPv4Address,
    IPv6Address,
    IPVersion,
)
from app.models.radius import RadiusUser
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

_RADIUS_TABLES = ("radcheck", "radreply")

# Literal SQL per table (no name interpolation) — matches radius_population's
# convention so the SQL-injection lint (S608) stays a real gate, not a noqa.
_RADIUS_SELECT = {
    "radcheck": (
        "SELECT username, attribute, op, value FROM radcheck "
        "WHERE username = ANY(%s) ORDER BY username, id"
    ),
    "radreply": (
        "SELECT username, attribute, op, value FROM radreply "
        "WHERE username = ANY(%s) ORDER BY username, id"
    ),
}
_RADIUS_DELETE = {
    "radcheck": "DELETE FROM radcheck WHERE username = ANY(%s)",
    "radreply": "DELETE FROM radreply WHERE username = ANY(%s)",
}
_RADIUS_INSERT = {
    "radcheck": (
        "INSERT INTO radcheck (username, attribute, op, value) "
        "VALUES (%s, %s, %s, %s)"
    ),
    "radreply": (
        "INSERT INTO radreply (username, attribute, op, value) "
        "VALUES (%s, %s, %s, %s)"
    ),
}


# ---------------------------------------------------------------------------
# Reads (best-effort, no mutation)
# ---------------------------------------------------------------------------


def _subscriber_usernames(db: Session, subscriber_id: Any) -> list[str]:
    """Every RADIUS username that belongs to this subscriber: credential
    usernames ∪ radius_user usernames ∪ subscription logins. Deduped, non-empty,
    stable order."""
    sid = coerce_uuid(subscriber_id)
    names: list[str] = []
    seen: set[str] = set()

    def _add(value: str | None) -> None:
        v = (value or "").strip()
        if v and v not in seen:
            seen.add(v)
            names.append(v)

    for username in db.scalars(
        select(AccessCredential.username).where(
            AccessCredential.subscriber_id == sid
        )
    ).all():
        _add(username)
    for username in db.scalars(
        select(RadiusUser.username).where(RadiusUser.subscriber_id == sid)
    ).all():
        _add(username)
    for login in db.scalars(
        select(Subscription.login).where(Subscription.subscriber_id == sid)
    ).all():
        _add(login)
    return names


def _read_radius_rows(
    usernames: list[str],
) -> tuple[list[dict] | None, list[dict] | None, str | None]:
    """Read radcheck/radreply rows for ``usernames`` from the canonical RADIUS
    DB. Returns ``(radcheck, radreply, error)``; on any failure returns
    ``(None, None, error)`` so capture degrades to a partial backup."""
    if not usernames:
        return [], [], None
    try:
        import psycopg

        from app.services.radius_dsn import radius_dsn_libpq

        dsn = radius_dsn_libpq()
        if not dsn:
            return None, None, "radius_dsn_unconfigured"
        out: dict[str, list[dict]] = {}
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            for table in _RADIUS_TABLES:
                cur = conn.execute(_RADIUS_SELECT[table], (usernames,))
                out[table] = [
                    {
                        "username": r[0],
                        "attribute": r[1],
                        "op": r[2],
                        "value": r[3],
                    }
                    for r in cur.fetchall()
                ]
        return out["radcheck"], out["radreply"], None
    except Exception as exc:  # best-effort: a partial backup beats no mutation
        logger.warning("connectivity backup: RADIUS read failed: %s", exc)
        return None, None, str(exc)[:200]


def _capture_credentials(db: Session, subscriber_id: Any) -> list[dict]:
    sid = coerce_uuid(subscriber_id)
    radius_active: dict[str, bool] = {}
    for username, is_active in db.execute(
        select(RadiusUser.username, RadiusUser.is_active).where(
            RadiusUser.subscriber_id == sid
        )
    ).all():
        radius_active[username] = bool(is_active)
    creds: list[dict] = []
    for cred in db.scalars(
        select(AccessCredential).where(AccessCredential.subscriber_id == sid)
    ).all():
        creds.append(
            {
                "credential_id": str(cred.id),
                "username": cred.username,
                "credential_active": bool(cred.is_active),
                "radius_user_active": radius_active.get(cred.username),
            }
        )
    return creds


def _capture_ip_state(db: Session, subscriber_id: Any) -> dict[str, Any]:
    sid = coerce_uuid(subscriber_id)
    subs = [
        {
            "id": str(s.id),
            "status": s.status.value if s.status is not None else None,
            "ipv4_address": s.ipv4_address,
            "ipv6_address": s.ipv6_address,
        }
        for s in db.scalars(
            select(Subscription).where(Subscription.subscriber_id == sid)
        ).all()
    ]
    assignments: list[dict] = []
    rows = db.execute(
        select(
            IPAssignment.id,
            IPAssignment.ip_version,
            IPAssignment.is_active,
            IPAssignment.allocation_type,
            IPv4Address.address,
            IPv6Address.address,
        )
        .select_from(IPAssignment)
        .outerjoin(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
        .outerjoin(IPv6Address, IPAssignment.ipv6_address_id == IPv6Address.id)
        .where(IPAssignment.subscriber_id == sid)
    ).all()
    for aid, ver, active, alloc, v4, v6 in rows:
        assignments.append(
            {
                "id": str(aid),
                "ip_version": ver.value if ver is not None else None,
                "address": v4 if ver == IPVersion.ipv4 else v6,
                "is_active": bool(active),
                "allocation_type": alloc,
            }
        )
    return {"subscriptions": subs, "assignments": assignments}


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def capture_connectivity_state(
    db: Session,
    subscriber_id: Any,
    *,
    reason: str,
    captured_by: str | None = None,
    include_radius: bool = True,
) -> ConnectivityStateBackup | None:
    """Snapshot a subscriber's connectivity state into a backup row.

    Best-effort: returns ``None`` (logging) on failure rather than raising, so it
    is safe to call inline before a destructive mutation. The row is added to the
    caller's session and flushed (so it shares the mutation's transaction — if
    the mutation rolls back, so does the unneeded backup), but NOT committed here.
    """
    try:
        sid = coerce_uuid(subscriber_id)
        usernames = _subscriber_usernames(db, sid)
        radcheck = radreply = None
        radius_error = None
        if include_radius:
            radcheck, radreply, radius_error = _read_radius_rows(usernames)
        credentials = _capture_credentials(db, sid)
        ip_state = _capture_ip_state(db, sid)

        backup = ConnectivityStateBackup(
            subscriber_id=sid,
            reason=reason,
            captured_by=captured_by,
            radcheck=radcheck,
            radreply=radreply,
            credentials=credentials,
            ip_state=ip_state,
        )
        db.add(backup)
        db.flush()

        db.add(
            AuditEvent(
                action="connectivity_backup.capture",
                entity_type="subscriber",
                entity_id=str(sid),
                metadata_={
                    "backup_id": str(backup.id),
                    "reason": reason,
                    "usernames": len(usernames),
                    "radcheck_rows": len(radcheck) if radcheck is not None else None,
                    "radreply_rows": len(radreply) if radreply is not None else None,
                    "credentials": len(credentials),
                    "radius_error": radius_error,
                },
            )
        )
        return backup
    except Exception as exc:  # never break the guarded mutation
        logger.warning(
            "connectivity backup: capture failed for subscriber=%s reason=%s: %s",
            subscriber_id,
            reason,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Restore (operator rollback — dry-run by default)
# ---------------------------------------------------------------------------


def _restore_radius_rows(backup: ConnectivityStateBackup) -> dict[str, int]:
    """Re-materialize the captured radcheck/radreply rows: delete the current
    rows for the captured usernames, then insert exactly what was captured. Only
    touches usernames present in the backup."""
    import psycopg

    from app.services.radius_dsn import radius_dsn_libpq

    captured = {"radcheck": backup.radcheck or [], "radreply": backup.radreply or []}
    usernames = sorted(
        {r["username"] for rows in captured.values() for r in rows if r.get("username")}
    )
    counts = {"radcheck_restored": 0, "radreply_restored": 0}
    if not usernames:
        return counts
    dsn = radius_dsn_libpq()
    if not dsn:
        raise RuntimeError("RADIUS database DSN not configured")
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        for table in _RADIUS_TABLES:
            conn.execute(_RADIUS_DELETE[table], (usernames,))
            rows = captured[table]
            if rows:
                conn.cursor().executemany(
                    _RADIUS_INSERT[table],
                    [(r["username"], r["attribute"], r["op"], r["value"]) for r in rows],
                )
            counts[f"{table}_restored"] = len(rows)
        conn.commit()
    return counts


def restore_connectivity_state(
    db: Session,
    backup_id: Any,
    *,
    dry_run: bool = True,
    restored_by: str | None = None,
    include_radius: bool = True,
) -> dict[str, Any]:
    """Restore a captured backup. **Dry-run by default** — returns the plan and
    writes nothing. With ``dry_run=False`` it flips local credential/IP flags and
    subscription IP columns back to the captured values and (``include_radius``)
    re-materializes the captured radcheck/radreply rows."""
    backup = db.get(ConnectivityStateBackup, coerce_uuid(backup_id))
    if backup is None:
        return {"ok": False, "reason": "backup_not_found"}

    plan: dict[str, Any] = {
        "ok": True,
        "backup_id": str(backup.id),
        "subscriber_id": str(backup.subscriber_id),
        "dry_run": dry_run,
        "credentials": len(backup.credentials or []),
        "assignments": len((backup.ip_state or {}).get("assignments", [])),
        "subscriptions": len((backup.ip_state or {}).get("subscriptions", [])),
        "radcheck_rows": len(backup.radcheck or []) if backup.radcheck else 0,
        "radreply_rows": len(backup.radreply or []) if backup.radreply else 0,
        "applied": False,
    }
    if dry_run:
        return plan

    # Local flags: AccessCredential.is_active / RadiusUser.is_active.
    by_username_cred = {c["username"]: c for c in (backup.credentials or [])}
    for cred in db.scalars(
        select(AccessCredential).where(
            AccessCredential.subscriber_id == backup.subscriber_id
        )
    ).all():
        snap = by_username_cred.get(cred.username)
        if snap is not None and snap.get("credential_active") is not None:
            cred.is_active = bool(snap["credential_active"])
    for ru in db.scalars(
        select(RadiusUser).where(RadiusUser.subscriber_id == backup.subscriber_id)
    ).all():
        snap = by_username_cred.get(ru.username)
        if snap is not None and snap.get("radius_user_active") is not None:
            ru.is_active = bool(snap["radius_user_active"])

    # IP state: subscription served columns + IPAssignment.is_active.
    ip_state = backup.ip_state or {}
    sub_snap = {s["id"]: s for s in ip_state.get("subscriptions", [])}
    for sub in db.scalars(
        select(Subscription).where(
            Subscription.subscriber_id == backup.subscriber_id
        )
    ).all():
        snap = sub_snap.get(str(sub.id))
        if snap is not None:
            sub.ipv4_address = snap.get("ipv4_address")
            sub.ipv6_address = snap.get("ipv6_address")
    assign_snap = {a["id"]: a for a in ip_state.get("assignments", [])}
    for assign in db.scalars(
        select(IPAssignment).where(
            IPAssignment.subscriber_id == backup.subscriber_id
        )
    ).all():
        snap = assign_snap.get(str(assign.id))
        if snap is not None and snap.get("is_active") is not None:
            assign.is_active = bool(snap["is_active"])

    radius_counts: dict[str, int] = {}
    if include_radius and (backup.radcheck is not None or backup.radreply is not None):
        radius_counts = _restore_radius_rows(backup)

    from datetime import UTC, datetime

    backup.restored_at = datetime.now(UTC)
    backup.restored_by = restored_by
    db.add(
        AuditEvent(
            action="connectivity_backup.restore",
            entity_type="subscriber",
            entity_id=str(backup.subscriber_id),
            metadata_={"backup_id": str(backup.id), **radius_counts},
        )
    )
    db.commit()

    plan["applied"] = True
    plan["radius"] = radius_counts
    return plan
