"""Read-only helpers for the ONT turn-up gaps (G3 / G9).

Owner: ``network.ont_service_configuration``. This module has NO writers of
any kind: it reads ACS queue state, RADIUS sessions, pending-create markers and
the credential owner's login ruling. The WAN service-port allocation (G1) lives
in the owner module. Every device write still goes lifecycle -> planner ->
applier.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.models.radius_active_session import RadiusActiveSession

logger = logging.getLogger(__name__)

WAITING_INFORM_CR_FAILED = "awaiting_inform_cr_failed"
WAITING_RADIUS_SESSION = "awaiting_radius_session"
# Waiting reasons a fresh Inform may resume with a write pass.
RESUMABLE_WAITING_REASONS = frozenset(
    {
        WAITING_INFORM_CR_FAILED,
        "awaiting_acs_task_drain",
        "awaiting_fresh_device_readback",
    }
)
RADIUS_SESSION_TIMEOUT_SECONDS = 15 * 60
RADIUS_SESSION_MAX_ATTEMPTS = 15
RADIUS_SESSION_RECHECK_SECONDS = 60


class WanServicePortAmbiguous(Exception):
    """More than one candidate WAN service-port allocation exists."""


def _linked_cpe(db: Session, ont_id: UUID) -> Any:
    from app.models.tr069 import Tr069CpeDevice

    return db.scalars(
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.ont_unit_id == ont_id)
        .where(Tr069CpeDevice.is_active.is_(True))
        .limit(1)
    ).first()


def acs_queued_task_count(db: Session, ont: OntUnit) -> int | None:
    """Number of GenieACS tasks still queued for the ONT; ``None`` if unknown.

    Read-only. Unknown (no device link, ACS unreachable) must be treated by
    callers as "do not assume anything" (fail closed).
    """
    try:
        from app.models.tr069 import Tr069AcsServer
        from app.services.genieacs_client import create_genieacs_client

        cpe = _linked_cpe(db, ont.id)
        device_id = getattr(cpe, "genieacs_device_id", None)
        if cpe is None or not device_id:
            return None
        server = db.get(Tr069AcsServer, cpe.acs_server_id)
        if server is None or not server.base_url:
            return None
        tasks = create_genieacs_client(server.base_url).get_pending_tasks(device_id)
        return len([t for t in tasks if isinstance(t, dict)])
    except Exception:  # noqa: BLE001
        logger.info("acs_queue_lookup_failed", extra={"ont_id": str(ont.id)})
        return None


PENDING_CREATE_TTL_SECONDS = 10 * 60
MAX_RESUMES_PER_REVISION = 3


def _fresh(value: Any, now: datetime) -> bool:
    try:
        ts = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return False
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts).total_seconds() < PENDING_CREATE_TTL_SECONDS


def has_pending_create_marker(ont: OntUnit, now: datetime | None = None) -> bool:
    """Whether a WCD/PPP-child creation is still pending (#3758 markers).

    Markers older than the TTL are ignored: the applier no longer treats them
    as pending either, so an expired marker must not keep resuming a head.
    """
    now = now or datetime.now(UTC)
    snapshot = getattr(ont, "tr069_last_snapshot", None)
    caps = snapshot.get("capabilities") if isinstance(snapshot, dict) else None
    if not isinstance(caps, dict):
        return False
    if _fresh(caps.get("pending_wcd_add"), now):
        return True
    pending = caps.get("pending_actions")
    if isinstance(pending, dict):
        return any(
            isinstance(v, dict)
            and v.get("state") == "pending_verification"
            and _fresh(v.get("requested_at"), now)
            for k, v in pending.items()
            if str(k).startswith("add_")
        )
    return False


PPP_WRITE_FIELDS = frozenset({"acs_pppoe", "omci_pppoe", "ont_reset"})


MAX_MAC_OFFSET = 8


def _mac_int(value: str | None) -> int | None:
    digits = "".join(ch for ch in str(value or "").lower() if ch in "0123456789abcdef")
    return int(digits, 16) if len(digits) == 12 else None


def mac_matches(
    session_mac: str | None, observed_wan_mac: str | None, ont_mac: str | None
) -> bool:
    """Exact match on the observed WAN PPP MAC when known; otherwise within a
    small offset (<= 8) of the ONT MAC. Neighbouring ONTs share the first five
    octets, so a prefix match is NOT enough. No known MAC means no match."""
    got = _mac_int(session_mac)
    if got is None:
        return False
    exact = _mac_int(observed_wan_mac)
    if exact is not None:
        return got == exact
    base = _mac_int(ont_mac)
    return base is not None and abs(got - base) <= MAX_MAC_OFFSET


def radius_session_live(
    db: Session,
    *,
    credential_id: UUID,
    subscription_id: UUID,
    username: str,
    ppp_write_issued_at: datetime | None,
    ont_mac: str | None,
    observed_wan_mac: str | None = None,
) -> bool:
    """A live session for the canonical credential that can prove this revision.

    If this revision issued a PPP write/Reset, the session must have started
    after it. Otherwise (no-op re-apply) an existing session is accepted, but
    its calling-station MAC must match the observed WAN MAC exactly, or lie
    within a small offset of the ONT MAC; with no known MAC it keeps waiting.
    """
    from sqlalchemy import and_, or_

    stmt = select(RadiusActiveSession).where(
        or_(
            RadiusActiveSession.access_credential_id == credential_id,
            and_(
                RadiusActiveSession.subscription_id == subscription_id,
                RadiusActiveSession.username == username,
            ),
        )
    )
    if ppp_write_issued_at is not None:
        stmt = stmt.where(RadiusActiveSession.session_start >= ppp_write_issued_at)
    sessions = list(db.scalars(stmt.limit(20)))
    if ppp_write_issued_at is not None:
        return bool(sessions)
    return any(
        mac_matches(x.calling_station_id, observed_wan_mac, ont_mac) for x in sessions
    )


def radius_gate(
    db: Session,
    *,
    desired_mode: str | None,
    desired_login: str | None,
    subscription_id: UUID | None,
    ppp_write_issued_at: datetime | None,
    ont_mac: str | None,
    observed_wan_mac: str | None = None,
    waiting_since: datetime | None,
    attempt: int,
    now: datetime | None = None,
) -> str:
    """``ok`` | ``waiting`` | ``timeout`` | ``login_mismatch`` |
    ``credential_ambiguous`` for the WAN RADIUS proof.

    ACS ConnectionStatus is a known false negative on HG8546M and is not
    consulted.
    """
    if desired_mode != "pppoe":
        return "ok"
    from app.services.cpe_dialer_credential_reconcile import (
        DialerCredentialRefusal,
        authoritative_dialer_login,
    )

    ruling = authoritative_dialer_login(
        db, subscription_id=subscription_id, login=desired_login
    )
    if ruling.refusal is DialerCredentialRefusal.ambiguous:
        return "credential_ambiguous"
    if (
        ruling.refusal is not None
        or not ruling.login_matches
        or ruling.credential_id is None
        or subscription_id is None
        or not desired_login
    ):
        return "login_mismatch"
    if radius_session_live(
        db,
        credential_id=ruling.credential_id,
        subscription_id=subscription_id,
        username=desired_login.strip(),
        ppp_write_issued_at=ppp_write_issued_at,
        ont_mac=ont_mac,
        observed_wan_mac=observed_wan_mac,
    ):
        return "ok"
    now = now or datetime.now(UTC)
    if waiting_since is not None:
        if waiting_since.tzinfo is None:
            waiting_since = waiting_since.replace(tzinfo=UTC)
        if (now - waiting_since).total_seconds() >= RADIUS_SESSION_TIMEOUT_SECONDS:
            return "timeout"
    if attempt >= RADIUS_SESSION_MAX_ATTEMPTS:
        return "timeout"
    return "waiting"


def ppp_write_applied(result: Any) -> bool:
    """Whether the pass actually issued a PPP write or Reset (applied evidence)."""
    return any(
        str(getattr(a, "field", "")) in PPP_WRITE_FIELDS
        for a in getattr(result, "actions_applied", ()) or ()
    )
