from datetime import datetime, timezone

from decimal import Decimal

from app.models.network_monitoring import Alert, AlertRule, MetricType
from app.models.notification import NotificationChannel, DeliveryStatus
from app.schemas.billing import InvoiceBulkVoidRequest, InvoiceBulkWriteOffRequest, InvoiceCreate
from app.schemas.network_monitoring import AlertAcknowledgeRequest, AlertRuleBulkUpdateRequest
from app.schemas.notification import NotificationBulkCreateRequest, NotificationDeliveryCreate, NotificationDeliveryBulkUpdateRequest, NotificationCreate
from app.schemas.tickets import TicketBulkUpdateRequest, TicketCommentBulkCreateRequest, TicketCreate, TicketUpdate
from app.services import billing as billing_service
from app.services import network_monitoring as monitoring_service
from app.services import notification as notification_service
from app.services import tickets as tickets_service


def test_bulk_update_tickets(db_session, subscriber_account):
    ticket1 = tickets_service.tickets.create(
        db_session,
        TicketCreate(
            account_id=subscriber_account.id,
            title="Issue 1",
        ),
    )
    ticket2 = tickets_service.tickets.create(
        db_session,
        TicketCreate(
            account_id=subscriber_account.id,
            title="Issue 2",
        ),
    )
    updated = tickets_service.tickets.bulk_update(
        db_session,
        [str(ticket1.id), str(ticket2.id)],
        TicketUpdate(status="resolved", resolved_at=datetime.now(timezone.utc)),
    )
    assert updated == 2
    refreshed = tickets_service.tickets.get(db_session, str(ticket1.id))
    assert refreshed.status.value == "resolved"


def test_bulk_ticket_comments(db_session, subscriber_account):
    ticket1 = tickets_service.tickets.create(
        db_session,
        TicketCreate(
            account_id=subscriber_account.id,
            title="Issue 1",
        ),
    )
    ticket2 = tickets_service.tickets.create(
        db_session,
        TicketCreate(
            account_id=subscriber_account.id,
            title="Issue 2",
        ),
    )
    payload = TicketCommentBulkCreateRequest(
        ticket_ids=[ticket1.id, ticket2.id],
        body="Outage update",
        is_internal=False,
    )
    comments = tickets_service.ticket_comments.bulk_create(db_session, payload)
    assert len(comments) == 2


def test_bulk_acknowledge_alerts(db_session):
    rule = AlertRule(name="Uptime", metric_type=MetricType.uptime, threshold=0)
    db_session.add(rule)
    db_session.flush()
    alert1 = Alert(
        rule_id=rule.id,
        metric_type=MetricType.uptime,
        measured_value=0,
        triggered_at=datetime.now(timezone.utc),
    )
    alert2 = Alert(
        rule_id=rule.id,
        metric_type=MetricType.uptime,
        measured_value=0,
        triggered_at=datetime.now(timezone.utc),
    )
    db_session.add_all([alert1, alert2])
    db_session.commit()
    updated = monitoring_service.alerts.bulk_acknowledge(
        db_session,
        [str(alert1.id), str(alert2.id)],
        AlertAcknowledgeRequest(message="Outage"),
    )
    assert updated == 2


def test_bulk_update_alert_rules(db_session):
    rule1 = AlertRule(name="Rule 1", metric_type=MetricType.uptime, threshold=0)
    rule2 = AlertRule(name="Rule 2", metric_type=MetricType.uptime, threshold=0)
    db_session.add_all([rule1, rule2])
    db_session.commit()
    updated = monitoring_service.alert_rules.bulk_update(
        db_session,
        AlertRuleBulkUpdateRequest(rule_ids=[rule1.id, rule2.id], is_active=False),
    )
    assert updated == 2


def test_bulk_notifications_create(db_session):
    payload = NotificationBulkCreateRequest(
        channel=NotificationChannel.email,
        recipients=["a@example.com", "b@example.com"],
        subject="Outage update",
        body="We are investigating.",
    )
    notifications = notification_service.notifications.bulk_create(db_session, payload)
    assert len(notifications) == 2
    assert notifications[0].recipient == "a@example.com"


def test_bulk_notification_deliveries_update(db_session):
    notification = notification_service.notifications.create(
        db_session,
        NotificationCreate(
            channel=NotificationChannel.email,
            recipient="a@example.com",
            subject="Update",
            body="Body",
        ),
    )
    delivery1 = notification_service.deliveries.create(
        db_session,
        NotificationDeliveryCreate(
            notification_id=notification.id,
            status=DeliveryStatus.accepted,
        ),
    )
    delivery2 = notification_service.deliveries.create(
        db_session,
        NotificationDeliveryCreate(
            notification_id=notification.id,
            status=DeliveryStatus.accepted,
        ),
    )
    updated = notification_service.deliveries.bulk_update(
        db_session,
        NotificationDeliveryBulkUpdateRequest(
            delivery_ids=[delivery1.id, delivery2.id],
            status=DeliveryStatus.delivered,
        ),
    )
    assert updated == 2


def test_bulk_invoice_actions(db_session, subscriber_account):
    invoice1 = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            currency="USD",
            subtotal=Decimal("100.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
        ),
    )
    invoice2 = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            currency="USD",
            subtotal=Decimal("50.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("50.00"),
            balance_due=Decimal("50.00"),
        ),
    )
    updated = billing_service.invoices.bulk_write_off(
        db_session,
        InvoiceBulkWriteOffRequest(invoice_ids=[invoice1.id, invoice2.id]),
    )
    assert updated == 2
    refreshed = billing_service.invoices.get(db_session, str(invoice1.id))
    assert refreshed.balance_due == Decimal("0.00")
    updated_void = billing_service.invoices.bulk_void(
        db_session,
        InvoiceBulkVoidRequest(invoice_ids=[invoice1.id]),
    )
    assert updated_void == 1
