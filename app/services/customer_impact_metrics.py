"""Customer-impact metrics (operations strategy: Customer Service SLA).

Fleet-level counters answering "how many customers are affected right now" —
computed from the same single-source-of-truth helpers the billing
suppression uses (``customer_service_state``), pushed to VictoriaMetrics as
trend series. Levels matter less than movement: a jump in
``customers_under_active_outage`` is the impact side of an incident, and
``customers_suppressed_billing_notice`` shows outage-aware comms working.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

HEARTBEAT_TASK = "customer_impact_metrics"

# Advisory-lock key for the single-flight metrics task ("cIm").
ADVISORY_LOCK_KEY = 0x63_49_6D

_vm_writer = None


def _writer():
    global _vm_writer
    if _vm_writer is None:
        from app.services.bandwidth_metrics_adapter import VictoriaMetricsWriter

        _vm_writer = VictoriaMetricsWriter()
    return _vm_writer


def collect_customer_impact(db: Session) -> dict[str, int]:
    """Count active subscriptions by current infrastructure-fault exposure."""
    from app.models.catalog import Subscription
    from app.models.subscriber import Subscriber
    from app.services.access_resolution import active_customer_service_filters
    from app.services.customer_service_state import (
        active_outage_subscription_ids,
        subscribers_with_open_infrastructure_down_tickets,
    )

    active = db.execute(
        select(Subscription.id, Subscription.subscriber_id)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .where(*active_customer_service_filters(Subscription, Subscriber))
    ).all()
    active_ids = {row.id for row in active}
    subscriber_ids = {row.subscriber_id for row in active if row.subscriber_id}

    outage_ids = active_outage_subscription_ids(db) & active_ids
    ticket_subscribers = subscribers_with_open_infrastructure_down_tickets(
        db, subscriber_ids
    )
    ticket_subscription_ids = {
        row.id for row in active if row.subscriber_id in ticket_subscribers
    }

    return {
        "active_subscriptions": len(active_ids),
        "customers_under_active_outage": len(outage_ids),
        "customers_with_open_infra_ticket": len(ticket_subscription_ids),
        "customers_suppressed_billing_notice": len(
            outage_ids | ticket_subscription_ids
        ),
    }


def push_customer_impact_metrics(
    impact: dict, *, now: datetime | None = None
) -> dict[str, int]:
    """Push the impact counters to VictoriaMetrics as gauges."""
    ts_ms = int((now or datetime.now(UTC)).timestamp() * 1000)
    lines = [
        f"{name} {int(value)} {ts_ms}"
        for name, value in impact.items()
        if isinstance(value, int)
    ]
    if not lines:
        return {"impact_metric_lines": 0, "impact_metric_write_failed": 0}
    write_result = _writer().write_prometheus_lines(
        lines,
        adapter="customer.impact",
        operation="customer_impact",
    )
    return {
        "impact_metric_lines": len(lines),
        "impact_metric_write_failed": 0 if write_result.success else len(lines),
    }
