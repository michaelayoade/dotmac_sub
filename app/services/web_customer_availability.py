"""Admin (agent-facing) context for the per-customer availability report.

Presentation only. The number, its evidence and its method belong to
``topology.customer_availability``; this shapes them for one page and resolves
which of the customer's subscriptions the report is about.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.common import coerce_uuid

#: Periods an agent can ask for, mirroring the infrastructure SLA windows.
PERIODS: dict[str, int] = {"7d": 7, "30d": 30, "90d": 90}
DEFAULT_PERIOD = "30d"


def _humanize(seconds: int) -> str:
    if seconds <= 0:
        return "none"
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def customer_availability_context(
    db: Session, subscriber_id: str, *, period: str | None = None
) -> dict[str, Any]:
    """Build the availability report for a subscriber's active subscription."""
    from app.models.catalog import Subscription
    from app.services.topology.customer_availability import customer_availability

    period_key = period if period in PERIODS else DEFAULT_PERIOD
    days = PERIODS[period_key]

    account_id = coerce_uuid(subscriber_id)
    subscription = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == account_id)
        .order_by(Subscription.created_at.desc())
        .first()
    )

    if subscription is None:
        return {
            "availability": None,
            "availability_period": period_key,
            "availability_periods": list(PERIODS),
            "availability_subscriber_id": subscriber_id,
            "availability_message": (
                "No subscription found for this customer, so there is no "
                "service path to measure."
            ),
        }

    report = customer_availability(db, subscription, days=days)
    return {
        "availability": report,
        "availability_period": period_key,
        "availability_periods": list(PERIODS),
        "availability_subscriber_id": subscriber_id,
        "availability_humanize": _humanize,
        "availability_message": None,
    }
