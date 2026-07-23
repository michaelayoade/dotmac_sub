"""Outage-aware context for the customer ticket form.

`customer_service_state` named ticket deflection as a future consumer; this is
that consumer. Sub already knows, at the moment a customer opens the form,
whether their service is down because of an area outage it has detected, and
whether an infrastructure ticket for it is already open. Until now the form
asked them to describe it anyway, so a single fibre cut arrived as dozens of
individually-triaged tickets and each customer waited for an answer the NOC had
already given elsewhere.

**Deflection here means informing, never blocking.** The customer can always
raise a ticket. What changes is that they see what we already know first: the
incident, that engineers are on it, and where to follow it. A form that refuses
to accept a report during an outage would push the customer straight to
WhatsApp, which is the behaviour this is trying to reduce.

Nothing here decides connection state or outage truth — `customer_service_state`
and `topology.connection_status` own that. This module only shapes it for one
page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class TicketDeflection:
    """What the ticket form should tell the customer before they type."""

    #: True when Sub has independently detected a fault affecting this customer.
    known_issue: bool = False
    #: "outage" (area) or "trouble" (this customer's own last mile).
    scope: str | None = None
    headline: str = ""
    message: str = ""
    advice: str | None = None
    #: An already-open infrastructure ticket covering them, if any.
    existing_ticket_id: str | None = None
    #: The detected incident, when one has been declared.
    incident_id: str | None = None
    #: Suggested subject so a customer who proceeds files something triageable.
    suggested_title: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def as_context(self) -> dict[str, Any]:
        return {
            "deflection": self,
            "deflection_known_issue": self.known_issue,
        }


_NONE = TicketDeflection()


def assess_ticket_deflection(
    db: Session, customer: dict, *, session_data: dict | None = None
) -> TicketDeflection:
    """Assess what the customer's own service looks like right now.

    Never raises: the ticket form must render even when topology resolution
    fails. A customer who cannot report a problem because the diagnostic broke
    is strictly worse off than one who sees no banner.
    """
    try:
        return _assess(db, customer, session_data or customer)
    except Exception:
        logger.exception("Ticket deflection assessment failed; rendering plain form")
        return _NONE


def _assess(db: Session, customer: dict, session_data: dict) -> TicketDeflection:
    from app.services.customer_portal_context import resolve_customer_subscription
    from app.services.customer_service_state import get_customer_service_state
    from app.services.topology.connection_status import (
        STATE_CONNECTED,
        STATE_OUTAGE,
        assess,
    )

    subscription = resolve_customer_subscription(db, session_data)
    if subscription is None:
        return _NONE

    state = get_customer_service_state(db, subscription)
    if state.connection_state == STATE_CONNECTED:
        # Connected customers get the plain form. Whatever they are reporting,
        # we have nothing better to tell them than "describe it".
        return _NONE

    assessment = assess(db, subscription)
    is_area = state.connection_state == STATE_OUTAGE or state.area_outage

    return TicketDeflection(
        known_issue=True,
        scope="outage" if is_area else "trouble",
        headline=assessment.headline,
        message=assessment.message,
        advice=assessment.advice,
        existing_ticket_id=(
            str(state.open_infrastructure_ticket_id)
            if state.open_infrastructure_ticket_id
            else None
        ),
        incident_id=(str(state.active_outage_id) if state.active_outage_id else None),
        suggested_title=(
            "Service down — area outage" if is_area else "Service down — my connection"
        ),
    )
