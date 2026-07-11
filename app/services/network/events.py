"""Network event decision SOT.

Observation writers should not decide business consequences. This layer turns
resolved device/session/impact state into event decisions that callers can emit,
log, or ignore.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.events.types import EventType
from app.services.network.device_state import DeviceState
from app.services.network.outage_impact import OutageImpact
from app.services.network.radius_sessions import RadiusSessionResolution


@dataclass(frozen=True)
class NetworkEventDecision:
    should_emit: bool
    event_type: EventType | None
    reason: str
    payload: dict


def decide_device_state_event(
    *,
    previous_status: str | None,
    current: DeviceState,
) -> NetworkEventDecision:
    previous = (previous_status or "").strip().lower() or None
    now = current.live_status
    if previous == now:
        return NetworkEventDecision(False, None, "unchanged", {})
    if now == "down":
        return NetworkEventDecision(
            True,
            EventType.device_offline,
            "device_down",
            {
                "device_id": str(current.device_id),
                "device_name": current.name,
                "previous_status": previous,
                "current_status": now,
                "source": current.source,
            },
        )
    if now == "up" and previous in {"down", "problem"}:
        return NetworkEventDecision(
            True,
            EventType.device_online,
            "device_recovered",
            {
                "device_id": str(current.device_id),
                "device_name": current.name,
                "previous_status": previous,
                "current_status": now,
                "source": current.source,
            },
        )
    return NetworkEventDecision(False, None, "non_actionable_transition", {})


def decide_outage_event(
    *,
    impact: OutageImpact,
    alert_type: str,
) -> NetworkEventDecision:
    if not impact.has_customer_impact:
        return NetworkEventDecision(False, None, "no_customer_impact", {})
    return NetworkEventDecision(
        True,
        EventType.network_alert,
        "customer_impact",
        {
            "alert_type": alert_type,
            "scope_type": impact.scope_type,
            "scope_id": str(impact.scope_id),
            "affected_count": impact.affected_count,
        },
    )


def decide_radius_session_event(
    *,
    before_online: bool,
    current: RadiusSessionResolution,
) -> NetworkEventDecision:
    if before_online == current.is_online:
        return NetworkEventDecision(False, None, "unchanged", {})
    event_type = (
        EventType.session_started if current.is_online else EventType.session_ended
    )
    return NetworkEventDecision(
        True,
        event_type,
        "session_started" if current.is_online else "session_ended",
        {
            "subscriber_id": str(current.subscriber_id),
            "session_id": str(current.primary_session.id)
            if current.primary_session is not None
            else None,
            "network_identity": current.primary_identity.kind
            if current.primary_identity is not None
            else None,
        },
    )
