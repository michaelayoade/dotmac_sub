"""Operator/channel/settlement behavior; SQLite is not migration/RLS proof."""

from decimal import Decimal
from uuid import uuid4

import pytest
from dotmac_template_studio import service as studio
from dotmac_template_studio.models import Template, TemplateVersion
from fastapi import HTTPException

from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.event_store import EventStore
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationIntentCoverage,
    NotificationTemplate,
)
from app.models.payment_email import PaymentEmailCutover, PaymentEmailEpisode
from app.schemas.billing import PaymentCreate
from app.schemas.notification import NotificationTemplateUpdate
from app.services import billing as billing_service
from app.services import notification as notification_service
from app.services.billing.payment_receipt_identity import (
    payment_receipt_path,
    payment_receipt_reference,
)
from app.services.events.handlers.notification import NotificationHandler
from app.services.events.types import Event, EventType
from app.services.operator_tenant import operator_tenant_id
from app.services.owner_commands import CommandContext
from app.services.payment_email_cutover import (
    activate_payment_email_cutover,
    pause_payment_email_composition,
)
from app.services.payment_email_episodes import prepare_claimed_payment_email
from app.services.payment_template_adoption import (
    ReviewedPaymentEmailTemplates,
    adopt_payment_email_templates,
)
from app.services.payment_template_authoring import publish_payment_email_template
from app.services.settings_cache import SettingsCache


def _context():
    return CommandContext.system(
        actor="test:operator",
        scope=str(operator_tenant_id()),
        reason="reviewed test activation",
    )


@pytest.fixture
def adopted(db_session):
    from app.services.operator_tenant import provision_operator_tenant

    provision_operator_tenant(db_session)
    connection = db_session.connection()
    if "mod_tstudio" not in {
        row[1] for row in connection.exec_driver_sql("PRAGMA database_list")
    }:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS mod_tstudio")
    Template.__table__.create(connection, checkfirst=True)
    TemplateVersion.__table__.create(connection, checkfirst=True)
    receipt = NotificationTemplate(
        name="Receipt",
        code="payment_received_email",
        channel=NotificationChannel.email,
        subject="Receipt {receipt_number}",
        body="Hello {subscriber_name}; receipt {receipt_number}: {receipt_url}",
        conditions={},
        is_active=True,
    )
    paid = NotificationTemplate(
        name="Paid",
        code="invoice_paid_email",
        channel=NotificationChannel.email,
        subject="Invoice {invoice_number} paid",
        body="Hello {subscriber_name}; invoice {invoice_number} paid.",
        conditions={},
        is_active=True,
    )
    sms = NotificationTemplate(
        name="Receipt SMS",
        code="payment_received_sms",
        channel=NotificationChannel.sms,
        body="SMS receipt {receipt_number}",
        conditions={},
        is_active=True,
    )
    db_session.add_all((receipt, paid, sms))
    db_session.commit()
    reviewed = ReviewedPaymentEmailTemplates(receipt.id, paid.id)
    db_session.commit()
    adopt_payment_email_templates(db_session, context=_context(), reviewed=reviewed)
    return reviewed


def _activate(db, reviewed):
    db.commit()
    return activate_payment_email_cutover(db, context=_context(), reviewed=reviewed)


def _setting(db, key, *, value_text=None, value_json=None):
    row = (
        db.query(DomainSetting)
        .filter_by(domain=SettingDomain.notification, key=key)
        .one_or_none()
    )
    if row is None:
        row = DomainSetting(domain=SettingDomain.notification, key=key)
        db.add(row)
    row.value_type = (
        SettingValueType.json if value_json is not None else SettingValueType.boolean
    )
    row.value_text, row.value_json, row.is_active = value_text, value_json, True
    db.commit()
    SettingsCache.invalidate(SettingDomain.notification.value, key)


@pytest.mark.parametrize("email_active", (True, False))
def test_activated_receipt_retains_canonical_link_and_separate_sms(
    db_session, subscriber, adopted, email_active
):
    _activate(db_session, adopted)
    publish_payment_email_template(
        db_session,
        context=_context(),
        code="payment_received",
        expected_published_version=1,
        subject="Published receipt {receipt_number}",
        body="Published receipt {receipt_number}: {receipt_url}",
        is_active=email_active,
    )
    subscriber.phone = "+2348012345678"
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("100"),
        currency="NGN",
        status=PaymentStatus.succeeded,
    )
    db_session.add(payment)
    db_session.commit()
    _setting(db_session, "sms_enabled", value_text="true")
    _setting(
        db_session,
        "notification_channel_policy",
        value_json={"events": {"payment_received": ["email", "sms"]}},
    )
    event = Event(
        event_type=EventType.payment_received,
        account_id=subscriber.id,
        payload={
            "payment_id": str(payment.id),
            "amount": "100",
            "receipt_number": "FORGED",
            "receipt_url": "https://attacker.invalid/receipt",
        },
    )
    NotificationHandler().handle(db_session, event)
    db_session.flush()
    rows = db_session.query(Notification).all()
    assert {row.channel for row in rows} == (
        {NotificationChannel.email, NotificationChannel.sms}
        if email_active
        else {NotificationChannel.sms}
    )
    reference = payment_receipt_reference(payment.id)
    sms = next(row for row in rows if row.channel is NotificationChannel.sms)
    assert sms.body == f"SMS receipt {reference}"
    if email_active:
        email = next(row for row in rows if row.channel is NotificationChannel.email)
        assert email.subject == f"Published receipt {reference}"
        assert payment_receipt_path(payment.id) in email.body
        assert "FORGED" not in email.body and "attacker.invalid" not in email.body
    # Unallocated receipt has no proved invoice pair and remains individual.
    assert db_session.query(PaymentEmailEpisode).count() == 0


def test_sealed_delete_and_content_edit_refuse_before_mutation(db_session, adopted):
    _activate(db_session, adopted)
    receipt = db_session.get(NotificationTemplate, adopted.payment_received_legacy_id)
    original_body = receipt.body
    assert receipt.studio_content_sealed
    with pytest.raises(HTTPException) as deletion:
        notification_service.templates.delete(db_session, str(receipt.id))
    assert deletion.value.status_code == 409
    db_session.expire_all()
    assert db_session.get(NotificationTemplate, receipt.id).is_active is True
    with pytest.raises(HTTPException) as edit:
        notification_service.templates.update(
            db_session, str(receipt.id), NotificationTemplateUpdate(body="Replacement")
        )
    assert edit.value.status_code == 409
    assert db_session.get(NotificationTemplate, receipt.id).body == original_body


def test_activation_replay_preserves_studio_edit_and_paused_state(db_session, adopted):
    tenant_id = _activate(db_session, adopted)
    published = publish_payment_email_template(
        db_session,
        context=_context(),
        code="payment_received",
        expected_published_version=1,
        subject="Edited {receipt_number}",
        body="Edited receipt {receipt_number}: {receipt_url}",
    )
    assert published.published_version == 2
    db_session.commit()
    pause_payment_email_composition(db_session, context=_context())
    assert _activate(db_session, adopted) == tenant_id
    assert not db_session.get(PaymentEmailCutover, tenant_id).composition_enabled
    template = studio.get_by_slug(db_session, tenant_id, "payment-received", "email")
    assert template.published_version == 2


@pytest.mark.parametrize("state", ("dormant", "active", "paused"))
def test_real_settlement_paid_consequence_and_activation_gate(
    db_session, subscriber, adopted, state
):
    if state != "dormant":
        _activate(db_session, adopted)
    if state == "paused":
        pause_payment_email_composition(db_session, context=_context())
        cutover = db_session.get(PaymentEmailCutover, operator_tenant_id())
        assert not cutover.composition_enabled
        assert db_session.get(
            NotificationTemplate, adopted.payment_received_legacy_id
        ).studio_content_sealed
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-{uuid4()}",
        status=InvoiceStatus.issued,
        subtotal=Decimal("100"),
        total=Decimal("100"),
        balance_due=Decimal("100"),
        currency="NGN",
    )
    db_session.add(invoice)
    db_session.commit()
    payment = billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("100"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            allocations=[{"invoice_id": invoice.id, "amount": Decimal("100")}],
        ),
    )
    assert payment.status is PaymentStatus.succeeded
    assert invoice.status is InvoiceStatus.paid
    consequences = (
        db_session.query(EventStore)
        .filter_by(event_type=EventType.invoice_paid.value)
        .all()
    )
    assert len(consequences) == (1 if state == "active" else 0)
    if consequences:
        event = consequences[0]
        allocation = payment.allocations[0]
        assert event.invoice_id == invoice.id
        assert event.payload["payment_id"] == str(payment.id)
        assert event.payload["allocation_id"] == str(allocation.id)
        assert event.payload["ledger_entry_id"] == str(allocation.ledger_entry_id)
        assert event.payload["previous_status"] == "issued"
        assert event.payload["source"] == "payment_settlement"
        # Recompute/replay of an already paid invoice cannot invent a transition.
        from app.services.billing.payments import _finalize_invoice_payment_effects

        _finalize_invoice_payment_effects(
            db_session, invoice, causing_allocation=allocation
        )
        db_session.flush()
        assert (
            db_session.query(EventStore)
            .filter_by(event_type=EventType.invoice_paid.value)
            .count()
            == 1
        )


def test_activation_parity_failure_does_not_seal_either_legacy_row(db_session, adopted):
    from app.services.domain_errors import DomainError

    receipt = db_session.get(NotificationTemplate, adopted.payment_received_legacy_id)
    receipt.body += " Changed after review"
    db_session.commit()
    with pytest.raises(DomainError) as failure:
        activate_payment_email_cutover(db_session, context=_context(), reviewed=adopted)
    assert failure.value.code == "payment_email_cutover.parity_failed"
    assert db_session.get(PaymentEmailCutover, operator_tenant_id()) is None
    assert not db_session.get(
        NotificationTemplate, adopted.payment_received_legacy_id
    ).studio_content_sealed
    assert not db_session.get(
        NotificationTemplate, adopted.invoice_paid_legacy_id
    ).studio_content_sealed


@pytest.mark.parametrize("receipt_first", (True, False))
def test_settlement_handler_cutover_pair_and_sms_replay(
    db_session, subscriber, adopted, receipt_first
):
    _activate(db_session, adopted)
    subscriber.phone = "+2348012345678"
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-{uuid4()}",
        status=InvoiceStatus.issued,
        subtotal=Decimal("100"),
        total=Decimal("100"),
        balance_due=Decimal("100"),
        currency="NGN",
    )
    db_session.add(invoice)
    db_session.commit()
    _setting(db_session, "sms_enabled", value_text="true")
    _setting(
        db_session,
        "notification_channel_policy",
        value_json={
            "events": {"payment_received": ["email", "sms"], "invoice_paid": ["email"]}
        },
    )
    payment = billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("100"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            allocations=[{"invoice_id": invoice.id, "amount": Decimal("100")}],
        ),
    )
    stored = (
        db_session.query(EventStore)
        .filter(
            EventStore.event_type.in_(
                (EventType.payment_received.value, EventType.invoice_paid.value)
            )
        )
        .all()
    )
    assert {row.event_type for row in stored} == {
        EventType.payment_received.value,
        EventType.invoice_paid.value,
    }
    events = [
        Event(
            event_id=row.event_id,
            event_type=EventType(row.event_type),
            payload=row.payload,
            account_id=row.account_id,
            invoice_id=row.invoice_id,
        )
        for row in stored
    ]
    events.sort(
        key=lambda event: (
            (event.event_type is EventType.payment_received) != receipt_first
        )
    )
    handler = NotificationHandler()
    for event in events:
        handler.handle(db_session, event)
        db_session.flush()
    rows = db_session.query(Notification).all()
    email = [row for row in rows if row.channel is NotificationChannel.email]
    sms = [row for row in rows if row.channel is NotificationChannel.sms]
    assert len(email) == len(sms) == 1
    assert payment_receipt_reference(payment.id) in email[0].subject
    assert payment_receipt_path(payment.id) in email[0].body
    assert invoice.invoice_number in email[0].body
    assert sms[0].body == f"SMS receipt {payment_receipt_reference(payment.id)}"
    assert db_session.query(PaymentEmailEpisode).count() == 1
    for event in events:
        handler.handle(db_session, event)
    db_session.flush()
    assert db_session.query(Notification).count() == 2


def test_paused_pair_drains_frozen_content_and_new_linked_receipt_is_individual(
    db_session, subscriber, adopted
):
    _activate(db_session, adopted)
    subscriber.phone = "+2348012345678"
    _setting(db_session, "sms_enabled", value_text="true")
    _setting(
        db_session,
        "notification_channel_policy",
        value_json={
            "events": {"payment_received": ["email", "sms"], "invoice_paid": ["email"]}
        },
    )

    def settle_and_events(amount="100"):
        invoice = Invoice(
            account_id=subscriber.id,
            invoice_number=f"INV-{uuid4()}",
            status=InvoiceStatus.issued,
            subtotal=Decimal(amount),
            total=Decimal(amount),
            balance_due=Decimal(amount),
            currency="NGN",
        )
        db_session.add(invoice)
        db_session.commit()
        payment = billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal(amount),
                currency="NGN",
                status=PaymentStatus.succeeded,
                allocations=[{"invoice_id": invoice.id, "amount": Decimal(amount)}],
            ),
        )
        rows = [
            row
            for row in db_session.query(EventStore).all()
            if row.payload.get("payment_id") == str(payment.id)
            and row.event_type
            in (EventType.payment_received.value, EventType.invoice_paid.value)
        ]
        assert any(
            row.event_type == EventType.payment_received.value
            and row.invoice_id == invoice.id
            for row in rows
        )
        return payment, invoice, rows

    first_payment, first_invoice, first_rows = settle_and_events()
    assert {row.event_type for row in first_rows} == {
        EventType.payment_received.value,
        EventType.invoice_paid.value,
    }
    handler = NotificationHandler()
    for row in first_rows:
        handler.handle(
            db_session,
            Event(
                event_id=row.event_id,
                event_type=EventType(row.event_type),
                payload=row.payload,
                account_id=row.account_id,
                invoice_id=row.invoice_id,
            ),
        )
    db_session.flush()
    first_email = (
        db_session.query(Notification)
        .filter_by(channel=NotificationChannel.email)
        .one()
    )
    frozen_subject, frozen_body = first_email.subject, first_email.body
    assert first_invoice.invoice_number in frozen_body
    assert payment_receipt_path(first_payment.id) in frozen_body
    assert db_session.query(PaymentEmailEpisode).count() == 1
    assert (
        db_session.query(NotificationIntentCoverage)
        .filter_by(notification_id=first_email.id)
        .count()
        == 2
    )

    db_session.commit()
    pause_payment_email_composition(db_session, context=_context())
    publish_payment_email_template(
        db_session,
        context=_context(),
        code="payment_received",
        expected_published_version=1,
        subject="Updated receipt {receipt_number}",
        body="Updated receipt {receipt_number}: {receipt_url}",
    )
    publish_payment_email_template(
        db_session,
        context=_context(),
        code="invoice_paid",
        expected_published_version=1,
        subject="Updated invoice {invoice_number} paid",
        body="Updated invoice {invoice_number} paid.",
    )
    prepare_claimed_payment_email(db_session, first_email)
    db_session.flush()
    assert (first_email.subject, first_email.body) == (frozen_subject, frozen_body)
    assert (
        db_session.query(NotificationIntentCoverage)
        .filter_by(notification_id=first_email.id)
        .count()
        == 2
    )

    second_payment, second_invoice, second_rows = settle_and_events("150")
    assert {row.event_type for row in second_rows} == {EventType.payment_received.value}
    receipt = second_rows[0]
    assert receipt.invoice_id == second_invoice.id
    handler.handle(
        db_session,
        Event(
            event_id=receipt.event_id,
            event_type=EventType.payment_received,
            payload=receipt.payload,
            account_id=receipt.account_id,
            invoice_id=receipt.invoice_id,
        ),
    )
    db_session.flush()
    emails = (
        db_session.query(Notification)
        .filter_by(channel=NotificationChannel.email)
        .all()
    )
    sms = (
        db_session.query(Notification).filter_by(channel=NotificationChannel.sms).all()
    )
    assert len(emails) == len(sms) == 2
    second_email = next(row for row in emails if row.id != first_email.id)
    assert second_email.subject == (
        f"Updated receipt {payment_receipt_reference(second_payment.id)}"
    )
    assert payment_receipt_path(second_payment.id) in second_email.body
    assert second_invoice.invoice_number not in second_email.body
    assert db_session.query(PaymentEmailEpisode).count() == 1
    assert (
        db_session.query(NotificationIntentCoverage)
        .filter_by(notification_id=second_email.id)
        .count()
        == 1
    )
    assert {row.body for row in sms} == {
        f"SMS receipt {payment_receipt_reference(payment_id)}"
        for payment_id in (first_payment.id, second_payment.id)
    }
