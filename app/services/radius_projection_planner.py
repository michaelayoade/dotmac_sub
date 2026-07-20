"""Plan customer RADIUS projection from shared billing/access state."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.catalog import AccessState
from app.models.enforcement_lock import AccessRestrictionMode
from app.services.access_resolution import (
    CustomerBillingAccessState,
    resolve_customer_access,
)


@dataclass(frozen=True)
class RadiusProjectionPlan:
    """Decision consumed by RADIUS writers.

    ``mode`` maps directly to radcheck/radreply behavior:
    - active: write Cleartext-Password and normal radreply
    - captive: write Cleartext-Password and walled-garden radreply
    - reject: write Auth-Type := Reject and no radreply
    - none: no RADIUS projection expected
    """

    mode: str
    access_state: AccessState | None
    blocked: bool
    radius_allowed: bool
    write_password: bool
    write_radreply: bool
    captive: bool
    block_reason: str | None
    billing_access_state: CustomerBillingAccessState


def plan_radius_projection(
    subscription,
    *,
    restriction_mode: AccessRestrictionMode | None = None,
) -> RadiusProjectionPlan:
    decision = resolve_customer_access(
        subscription,
        access_restriction_mode=restriction_mode,
    )
    state = decision.state
    mode = state.radius_mode
    return RadiusProjectionPlan(
        mode=mode,
        access_state=state.radius_access_state,
        blocked=state.radius_blocked,
        radius_allowed=state.radius_allowed,
        write_password=mode in {"active", "captive"},
        write_radreply=mode in {"active", "captive"},
        captive=mode == "captive",
        block_reason=state.access_block_reason,
        billing_access_state=state,
    )
