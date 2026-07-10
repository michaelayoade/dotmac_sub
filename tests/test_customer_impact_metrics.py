"""Customer-impact counters and their VictoriaMetrics export."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.celery_app import celery_app
from app.models.support import Ticket, TicketStatus
from app.services import customer_impact_metrics
from tests.test_customer_plan_change_prepaid import _make_offer, _make_subscription

TASK_NAME = "app.tasks.customer_impact_metrics.export_customer_impact_metrics"


def test_task_registered_routed_and_exported():
    import app.tasks as tasks

    assert TASK_NAME in celery_app.tasks
    assert celery_app.conf.task_routes[TASK_NAME] == {"queue": "ingestion"}
    assert "export_customer_impact_metrics" in tasks.__all__


def _active_subscription(db_session, subscriber, name: str):
    offer = _make_offer(
        db_session, name=name, amount=Decimal("100.00"), plan_family="unlimited"
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        offer,
        next_billing_at=datetime.now(UTC) + timedelta(days=3),
        start_at=datetime.now(UTC) - timedelta(days=7),
    )
    db_session.commit()
    return subscription


def test_collect_counts_outage_ticket_and_union(db_session, subscriber, monkeypatch):
    from app.models.subscriber import Subscriber

    outage_sub = _active_subscription(db_session, subscriber, "CIM Outage")
    other = Subscriber(
        first_name="Impact", last_name="Ticket", email="cim-ticket@example.com"
    )
    db_session.add(other)
    db_session.commit()
    ticket_sub = _active_subscription(db_session, other, "CIM Ticket")
    _clean = _active_subscription(db_session, subscriber, "CIM Clean")

    db_session.add(
        Ticket(
            subscriber_id=other.id,
            title="Fiber cut on feeder",
            description="infrastructure down",
            status=TicketStatus.open.value,
            ticket_type="Infrastructure Down",
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.customer_service_state.active_outage_subscription_ids",
        lambda session: {outage_sub.id},
    )

    impact = customer_impact_metrics.collect_customer_impact(db_session)

    assert impact["active_subscriptions"] == 3
    assert impact["customers_under_active_outage"] == 1
    assert impact["customers_with_open_infra_ticket"] == 1
    assert impact["customers_suppressed_billing_notice"] == 2


def test_push_writes_gauges(monkeypatch):
    written = {}

    class _Writer:
        def write_prometheus_lines(self, lines, **kwargs):
            written["lines"] = lines
            written["kwargs"] = kwargs
            return SimpleNamespace(success=True, written=len(lines))

    monkeypatch.setattr(customer_impact_metrics, "_writer", lambda: _Writer())

    result = customer_impact_metrics.push_customer_impact_metrics(
        {
            "active_subscriptions": 2800,
            "customers_under_active_outage": 40,
            "customers_with_open_infra_ticket": 12,
            "customers_suppressed_billing_notice": 48,
        }
    )

    assert result == {"impact_metric_lines": 4, "impact_metric_write_failed": 0}
    assert any(
        line.startswith("customers_under_active_outage 40 ")
        for line in written["lines"]
    )
    assert written["kwargs"]["operation"] == "customer_impact"


def test_outage_helper_split_keeps_intersection_behavior(db_session, monkeypatch):
    from app.services import customer_service_state

    monkeypatch.setattr(
        customer_service_state,
        "active_outage_subscription_ids",
        lambda session: {"a", "b"},
    )
    subs = [SimpleNamespace(id="b"), SimpleNamespace(id="c")]
    assert customer_service_state.subscription_ids_under_active_outage(
        db_session, subs
    ) == {"b"}
