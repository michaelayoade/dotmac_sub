"""Read-only helpers for the ONT turn-up gaps (G1 / G3 / G9).

Owner: ``network.ont_service_configuration``. This module has NO device
writers: it allocates a service-port index in the database (existing
``service_port_allocator``), and it reads ACS queue state, RADIUS sessions and
pending-create markers. Every device write still goes lifecycle -> planner ->
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


def ensure_wan_service_port_index(
    db: Session, ont: OntUnit, *, wan_vlan: int, gem_index: int
) -> int | None:
    """Return the WAN service-port index, allocating and persisting if unset.

    Uses the existing DB-backed ``service_port_allocator``. Never touches or
    renumbers existing ports; when several internet allocations already exist
    for this ONT/VLAN/GEM it fails closed (returns ``None``) and the planner
    reports the drift instead of guessing.
    """
    from app.services.network.ont_desired_config import (
        desired_config,
        get_desired_config_value,
        set_desired_config_values,
    )
    from app.services.network.service_port_allocator import (
        AllocationError,
        allocate_service_port,
        find_allocation_by_correlation_key,
        get_allocations_for_ont,
    )

    olt_id = getattr(ont, "olt_device_id", None)
    if olt_id is None:
        return None
    recorded = get_desired_config_value(
        desired_config(ont), "olt", "wan_service_port_index"
    )
    if recorded not in (None, ""):
        return int(recorded)

    candidates = [
        a
        for a in get_allocations_for_ont(db, ont.id)
        if a.service_type == "internet"
        and a.vlan_id == wan_vlan
        and a.gem_index == gem_index
    ]
    if len(candidates) > 1:
        logger.warning(
            "wan_service_port_ambiguous",
            extra={"ont_id": str(ont.id), "candidates": len(candidates)},
        )
        return None
    if candidates:
        index = int(candidates[0].port_index)
    else:
        key = f"ont-wan-service-port:{ont.id}:{wan_vlan}:{gem_index}"
        allocation = find_allocation_by_correlation_key(db, key)
        try:
            if allocation is None:
                allocation = allocate_service_port(
                    db,
                    olt_id,
                    ont.id,
                    service_type="internet",
                    vlan_id=wan_vlan,
                    gem_index=gem_index,
                    correlation_key=key,
                )
        except AllocationError:
            logger.warning(
                "wan_service_port_allocation_failed",
                extra={"ont_id": str(ont.id)},
                exc_info=True,
            )
            return None
        index = int(allocation.port_index)
    set_desired_config_values(ont, {"olt.wan_service_port_index": index})
    db.flush()
    return index


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


def has_pending_create_marker(ont: OntUnit) -> bool:
    """Whether a WCD/PPP-child creation is still pending (#3758 markers)."""
    snapshot = getattr(ont, "tr069_last_snapshot", None)
    caps = snapshot.get("capabilities") if isinstance(snapshot, dict) else None
    if not isinstance(caps, dict):
        return False
    if caps.get("pending_wcd_add"):
        return True
    pending = caps.get("pending_actions")
    if isinstance(pending, dict):
        return any(
            isinstance(v, dict) and v.get("state") == "pending_verification"
            for k, v in pending.items()
            if str(k).startswith("add_")
        )
    return False


def radius_session_live(db: Session, login: str) -> bool:
    """A live RADIUS session exists for the PPPoE login."""
    return (
        db.scalars(
            select(RadiusActiveSession.id)
            .where(RadiusActiveSession.username == login)
            .limit(1)
        ).first()
        is not None
    )


def radius_gate(
    db: Session,
    *,
    desired_mode: str | None,
    login: str | None,
    waiting_since: datetime | None,
    attempt: int,
    now: datetime | None = None,
) -> str:
    """``ok`` | ``waiting`` | ``timeout`` for the WAN RADIUS proof.

    ACS ConnectionStatus is a known false negative on HG8546M and is not
    consulted. Non-PPPoE or login-less configurations have no RADIUS proof.
    """
    if desired_mode != "pppoe" or not login:
        return "ok"
    if radius_session_live(db, login):
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
