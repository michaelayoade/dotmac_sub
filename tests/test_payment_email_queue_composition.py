"""Content and queue behavior; SQLite does not prove concurrent claims or RLS."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.models.notification import (
    CommunicationIntentRecord,
    Notification,
    NotificationChannel,
    NotificationIntentCoverage,
    NotificationStatus,
    SuppressionReason,
    SuppressionScope,
)
from app.models.payment_email import PaymentEmailEpisode, PaymentEmailPart
from app.schemas.notification import NotificationDeliveryLatency
from app.services.communication_eligibility import suppress
from app.services.communication_intents import (
    CommunicationIntent,
    plan_intent,
    record_delivery_outcome,
)
from app.services.payment_email_content import (
    PaymentEmailKind,
    PublishedPaymentEmail,
    supports_plain_text_composition,
)
from app.services.payment_email_episodes import (
    PaymentEmailSource,
    ProvenPaymentPair,
    prepare_claimed_payment_email,
    stage_planned_payment_email,
    utc,
)


@pytest.fixture
def pair(db_session, subscriber):
    from app.services.operator_tenant import provision_operator_tenant

    provision_operator_tenant(db_session)
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-{uuid4()}",
        status=InvoiceStatus.paid,
        total=Decimal("100"),
        balance_due=Decimal("0"),
    )
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("100"),
        currency="NGN",
        status=PaymentStatus.succeeded,
    )
    db_session.add_all((invoice, payment))
    db_session.flush()
    return ProvenPaymentPair(payment.id, invoice.id, subscriber.id, uuid4())


def _plan(db, subscriber, kind, *, body=None, key=None, send_at=None, metadata=None):
    receipt = kind is PaymentEmailKind.receipt
    return plan_intent(
        db,
        CommunicationIntent(
            subscriber_id=subscriber.id,
            event_type="payment_received" if receipt else "invoice_paid",
            category="billing",
            subject="Receipt R-42" if receipt else "Invoice INV-42 paid",
            body=body
            or (
                "Receipt R-42: https://example.test/receipt/42"
                if receipt
                else "Invoice INV-42 paid."
            ),
            channels=(NotificationChannel.email,),
            recipients={NotificationChannel.email: subscriber.email},
            include_reseller=False,
            delivery_latency=NotificationDeliveryLatency.immediate,
            dedupe_key=key,
            send_at=send_at,
            metadata=metadata or {},
        ),
    )


def _source(pair, kind, decision):
    return PaymentEmailSource(
        pair,
        uuid4(),
        kind,
        PublishedPaymentEmail(uuid4(), 1, decision.subject, decision.body),
    )


@pytest.mark.parametrize("first_kind", tuple(PaymentEmailKind))
def test_each_source_is_durable_and_both_orders_share_one_delivery(
    db_session, subscriber, pair, first_kind
):
    second_kind = (
        PaymentEmailKind.invoice_paid
        if first_kind is PaymentEmailKind.receipt
        else PaymentEmailKind.receipt
    )
    first = _plan(db_session, subscriber, first_kind)
    second = _plan(db_session, subscriber, second_kind)
    a, b = first.recipients[0], second.recipients[0]
    one = stage_planned_payment_email(
        db_session, source=_source(pair, first_kind, a), recipient=a, now=a.planned_at
    )
    # Receipt exists durably before another event, a sweep or a timer runs.
    assert db_session.query(Notification).count() == 1
    assert db_session.query(NotificationIntentCoverage).count() == 1
    episode = db_session.query(PaymentEmailEpisode).one()
    assert utc(episode.deadline_at) == utc(a.planned_at) + timedelta(seconds=60)
    two = stage_planned_payment_email(
        db_session,
        source=_source(pair, second_kind, b),
        recipient=b,
        now=a.planned_at + timedelta(seconds=30),
    )
    assert two == one
    notification = db_session.get(Notification, one)
    assert notification.subject == "Receipt R-42"
    assert (
        notification.body
        == "Receipt R-42: https://example.test/receipt/42\n\nInvoice INV-42 paid."
    )
    assert db_session.query(PaymentEmailPart).count() == 2
    assert db_session.query(NotificationIntentCoverage).count() == 2
    notification.status = NotificationStatus.delivered
    record_delivery_outcome(db_session, notification)
    assert (
        db_session.get(CommunicationIntentRecord, first.intent_id).status == "delivered"
    )
    assert (
        db_session.get(CommunicationIntentRecord, second.intent_id).status
        == "delivered"
    )


def test_late_uncovered_invoice_is_individually_queued(db_session, subscriber, pair):
    first = _plan(db_session, subscriber, PaymentEmailKind.receipt).recipients[0]
    second = _plan(db_session, subscriber, PaymentEmailKind.invoice_paid).recipients[0]
    one = stage_planned_payment_email(
        db_session,
        source=_source(pair, PaymentEmailKind.receipt, first),
        recipient=first,
        now=first.planned_at,
    )
    two = stage_planned_payment_email(
        db_session,
        source=_source(pair, PaymentEmailKind.invoice_paid, second),
        recipient=second,
        now=first.planned_at + timedelta(seconds=60),
    )
    assert two != one
    assert db_session.get(Notification, one).body == first.body
    assert db_session.get(Notification, two).body == second.body
    assert db_session.query(NotificationIntentCoverage).count() == 2


def test_lock_wait_consumes_the_fixed_collection_window(
    db_session, subscriber, pair, monkeypatch
):
    from app.services import payment_email_episodes as episodes

    first = _plan(db_session, subscriber, PaymentEmailKind.receipt).recipients[0]
    second = _plan(db_session, subscriber, PaymentEmailKind.invoice_paid).recipients[0]
    one = stage_planned_payment_email(
        db_session,
        source=_source(pair, PaymentEmailKind.receipt, first),
        recipient=first,
        now=first.planned_at,
    )
    clock = [utc(first.planned_at) + timedelta(seconds=30)]

    class Clock:
        @staticmethod
        def now(_timezone):
            return clock[0]

    def waited_for_lock(*_args):
        clock[0] = utc(first.planned_at) + timedelta(seconds=61)

    monkeypatch.setattr(episodes, "datetime", Clock)
    monkeypatch.setattr(episodes, "_serialize_first_creator", waited_for_lock)
    two = stage_planned_payment_email(
        db_session,
        source=_source(pair, PaymentEmailKind.invoice_paid, second),
        recipient=second,
    )
    assert two != one
    assert db_session.get(Notification, one).body == first.body
    assert utc(db_session.query(PaymentEmailEpisode).one().deadline_at) == utc(
        first.planned_at
    ) + timedelta(seconds=60)


def test_replay_does_not_create_or_rewrite_physical_delivery(
    db_session, subscriber, pair
):
    plan = _plan(db_session, subscriber, PaymentEmailKind.receipt, key="receipt-replay")
    decision = plan.recipients[0]
    source = _source(pair, PaymentEmailKind.receipt, decision)
    one = stage_planned_payment_email(
        db_session, source=source, recipient=decision, now=decision.planned_at
    )
    replay = _plan(
        db_session,
        subscriber,
        PaymentEmailKind.receipt,
        body="Later edited body",
        key="receipt-replay",
    )
    two = stage_planned_payment_email(
        db_session,
        source=source,
        recipient=replay.recipients[0],
        now=decision.planned_at + timedelta(minutes=2),
    )
    assert two == one
    assert db_session.query(Notification).count() == 1
    assert db_session.get(Notification, one).body == decision.body


@pytest.mark.parametrize(
    "body",
    (
        "<!DOCTYPE html><html><body>Receipt R-42</body></html>",
        "<p>Receipt <a href='https://example.test/r'>R-42</a></p>",
    ),
)
def test_html_uses_unchanged_individual_delivery(db_session, subscriber, pair, body):
    decision = _plan(
        db_session, subscriber, PaymentEmailKind.receipt, body=body
    ).recipients[0]
    notification_id = stage_planned_payment_email(
        db_session,
        source=_source(pair, PaymentEmailKind.receipt, decision),
        recipient=decision,
    )
    assert db_session.get(Notification, notification_id).body == body
    assert db_session.query(PaymentEmailEpisode).count() == 0
    assert db_session.query(NotificationIntentCoverage).count() == 1


def test_opaque_transport_attachment_stays_individual_and_is_preserved(
    db_session, subscriber, pair
):
    envelope = [{"inbox_attachment_id": str(uuid4()), "filename": "receipt.png"}]
    plan = _plan(
        db_session,
        subscriber,
        PaymentEmailKind.receipt,
        metadata={"attachments": envelope},
    )
    recipient = plan.recipients[0]
    assert recipient.has_attachment_metadata
    assert recipient.attachments == ()
    notification_id = stage_planned_payment_email(
        db_session,
        source=_source(pair, PaymentEmailKind.receipt, recipient),
        recipient=recipient,
    )
    notification = db_session.get(Notification, notification_id)
    assert notification is not None
    assert notification.metadata_["attachments"] == envelope
    assert db_session.query(PaymentEmailEpisode).count() == 0


def test_suppression_before_claim_cancels_both_source_outcomes(
    db_session, subscriber, pair
):
    first = _plan(db_session, subscriber, PaymentEmailKind.receipt)
    second = _plan(db_session, subscriber, PaymentEmailKind.invoice_paid)
    a, b = first.recipients[0], second.recipients[0]
    one = stage_planned_payment_email(
        db_session,
        source=_source(pair, PaymentEmailKind.receipt, a),
        recipient=a,
        now=a.planned_at,
    )
    stage_planned_payment_email(
        db_session,
        source=_source(pair, PaymentEmailKind.invoice_paid, b),
        recipient=b,
        now=a.planned_at + timedelta(seconds=10),
    )
    suppress(
        db_session,
        subscriber_id=subscriber.id,
        channel=NotificationChannel.email,
        address=subscriber.email,
        scope=SuppressionScope.all,
        reason=SuppressionReason.bounce,
        note="test:customer suppression",
    )
    notification = db_session.get(Notification, one)
    prepare_claimed_payment_email(db_session, notification)
    assert notification.status is NotificationStatus.canceled
    record_delivery_outcome(db_session, notification)
    assert (
        db_session.get(CommunicationIntentRecord, first.intent_id).status
        == "suppressed"
    )
    assert (
        db_session.get(CommunicationIntentRecord, second.intent_id).status
        == "suppressed"
    )


@pytest.mark.parametrize(
    "claim_status",
    (NotificationStatus.queued, NotificationStatus.failed, NotificationStatus.sending),
)
def test_real_worker_suppression_projects_both_sources_before_generic_gate(
    db_session, subscriber, pair, monkeypatch, claim_status
):
    from app.tasks import notifications as notification_tasks

    first = _plan(db_session, subscriber, PaymentEmailKind.receipt)
    second = _plan(db_session, subscriber, PaymentEmailKind.invoice_paid)
    a, b = first.recipients[0], second.recipients[0]
    one = stage_planned_payment_email(
        db_session,
        source=_source(pair, PaymentEmailKind.receipt, a),
        recipient=a,
        now=a.planned_at,
    )
    stage_planned_payment_email(
        db_session,
        source=_source(pair, PaymentEmailKind.invoice_paid, b),
        recipient=b,
        now=a.planned_at + timedelta(seconds=10),
    )
    suppress(
        db_session,
        subscriber_id=subscriber.id,
        channel=NotificationChannel.email,
        address=subscriber.email,
        scope=SuppressionScope.all,
        reason=SuppressionReason.bounce,
        note="worker regression",
    )
    row = db_session.get(Notification, one)
    row.status = claim_status
    row.send_at = utc(a.planned_at) - timedelta(seconds=1)
    row.updated_at = utc(a.planned_at) - timedelta(hours=1)
    db_session.flush()

    def forbidden_transport(*_args, **_kwargs):
        raise AssertionError("Suppressed receipt reached the transport")

    monkeypatch.setattr(
        notification_tasks.communication_eligibility, "may_send", forbidden_transport
    )
    stats = notification_tasks._deliver_notification_queue_stats(
        db_session, notification_id=one
    )
    assert stats["suppressed"] == 1 and stats["delivered"] == 0
    assert row.status is NotificationStatus.canceled
    assert (
        db_session.get(CommunicationIntentRecord, first.intent_id).status
        == "suppressed"
    )
    assert (
        db_session.get(CommunicationIntentRecord, second.intent_id).status
        == "suppressed"
    )


def test_policy_incompatible_timing_is_not_merged(db_session, subscriber, pair):
    first = _plan(db_session, subscriber, PaymentEmailKind.receipt).recipients[0]
    later = utc(first.planned_at) + timedelta(hours=1)
    second = _plan(
        db_session, subscriber, PaymentEmailKind.invoice_paid, send_at=later
    ).recipients[0]
    one = stage_planned_payment_email(
        db_session,
        source=_source(pair, PaymentEmailKind.receipt, first),
        recipient=first,
        now=first.planned_at,
    )
    two = stage_planned_payment_email(
        db_session,
        source=_source(pair, PaymentEmailKind.invoice_paid, second),
        recipient=second,
        now=first.planned_at + timedelta(seconds=10),
    )
    assert two != one
    assert db_session.query(Notification).count() == 2
    assert utc(db_session.get(Notification, two).send_at) == later
    assert db_session.get(Notification, one).body == first.body


@pytest.mark.parametrize(
    "body, supported",
    (
        ("Total < 100; receipt https://example.test/r", True),
        ("<HTML><BODY>Receipt</BODY></HTML>", False),
        ("<a href='https://example.test/r'>Receipt</a>", False),
        ("", False),
    ),
)
def test_plain_text_boundary(body, supported):
    assert supports_plain_text_composition(body) is supported
