"""Lifecycle transitions must actually happen, and must be undoable.

Three strays from the 2026-07-13 re-audit:

L1  Vacation-hold resume passed an INTERPOLATED trigger ("admin:<uuid>") to a
    membership check against {"customer", "admin"}. It matched nothing, resolved
    zero locks, and the discarded False return let the UI report
    "Service resumed successfully" while the customer stayed offline.
L2  ServiceEntitlement was granted on payment and NEVER revoked — nothing in the
    codebase wrote void/reversed. A refund reopened the invoice while the
    entitlement it funded stayed active, so the customer kept the service they had
    been refunded for, free, for the whole period.
L3  set_topup_intent_status(intent, "failed") — "failed" was not a member, so the
    write raised ValueError, the surrounding commit never ran, and the
    idempotency-key release was rolled back: a declined card locked the customer
    out of retrying with a different one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber
from app.services.service_entitlements import (
    ensure_prepaid_entitlements_for_paid_invoice,
    revoke_prepaid_entitlements_for_unpaid_invoice,
)
from app.services.topup_intents import (
    TopupIntentStatus,
    set_topup_intent_status,
)

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def _account(db) -> Subscriber:
    sub = Subscriber(
        first_name="T",
        last_name="User",
        email=f"t{uuid.uuid4().hex[:8]}@example.com",
        status="active",
        is_active=True,
        billing_mode=BillingMode.prepaid,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _offer(db) -> CatalogOffer:
    offer = CatalogOffer(
        name=f"Offer {uuid.uuid4().hex[:6]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.prepaid,
        billing_cycle="monthly",
    )
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return offer


def _paid_prepaid_invoice(db, account, subscription) -> Invoice:
    invoice = Invoice(
        account_id=account.id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        status=InvoiceStatus.paid,
        total=Decimal("17500.00"),
        balance_due=Decimal("0.00"),
        currency="NGN",
        is_proforma=False,
        billing_period_start=NOW - timedelta(days=1),
        billing_period_end=NOW + timedelta(days=29),
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    db.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="prepaid period",
            quantity=Decimal("1"),
            unit_price=Decimal("17500.00"),
            amount=Decimal("17500.00"),
            is_active=True,
        )
    )
    db.commit()
    db.refresh(invoice)
    return invoice


def _entitlements(db, invoice) -> list[ServiceEntitlement]:
    return (
        db.query(ServiceEntitlement)
        .filter(ServiceEntitlement.source_invoice_id == invoice.id)
        .all()
    )


# --- L3: a declined card must not lock the customer out ----------------------


def test_failed_is_a_real_topup_status(db_session):
    """The saved-card path already passed "failed"; it just wasn't a member."""
    assert TopupIntentStatus.failed.value == "failed"


def test_marking_a_topup_failed_does_not_raise(db_session):
    """It raised ValueError, which rolled back the idempotency-key release."""
    from app.models.billing import TopupIntent

    intent = TopupIntent(
        account_id=_account(db_session).id,
        reference=f"ref-{uuid.uuid4().hex[:8]}",
        provider_type="paystack",
        requested_amount=Decimal("5000.00"),
        currency="NGN",
        status="pending",
    )
    db_session.add(intent)
    db_session.commit()

    changed = set_topup_intent_status(intent, "failed", source="saved_card_charge")
    db_session.commit()
    db_session.refresh(intent)

    assert changed is True
    assert intent.status == "failed"


def test_an_unknown_topup_status_is_still_rejected(db_session):
    """The validation itself must not be weakened."""
    from app.models.billing import TopupIntent

    intent = TopupIntent(
        account_id=_account(db_session).id,
        reference=f"ref-{uuid.uuid4().hex[:8]}",
        provider_type="paystack",
        requested_amount=Decimal("5000.00"),
        currency="NGN",
        status="pending",
    )
    db_session.add(intent)
    db_session.commit()

    with pytest.raises(ValueError):
        set_topup_intent_status(intent, "not_a_status", source="test")


# --- L2: a refunded customer must not keep the service -----------------------


def test_entitlement_is_revoked_when_the_invoice_stops_being_paid(db_session):
    account = _account(db_session)
    offer = _offer(db_session)
    subscription = Subscription(
        subscriber_id=account.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(subscription)
    db_session.commit()

    invoice = _paid_prepaid_invoice(db_session, account, subscription)
    granted = ensure_prepaid_entitlements_for_paid_invoice(db_session, invoice)
    db_session.commit()
    assert len(granted) == 1

    # The refund reopens the invoice.
    invoice.status = InvoiceStatus.issued
    invoice.balance_due = Decimal("17500.00")
    db_session.commit()

    revoked = revoke_prepaid_entitlements_for_unpaid_invoice(db_session, invoice)
    db_session.commit()

    assert len(revoked) == 1
    remaining_active = [
        e
        for e in _entitlements(db_session, invoice)
        if e.status == ServiceEntitlementStatus.active
    ]
    assert not remaining_active, (
        "the refunded customer kept an active entitlement — they keep the "
        "service they were refunded for, free, for the whole period"
    )


def test_revoking_leaves_a_still_paid_invoice_alone(db_session):
    """The guard must not revoke coverage the customer has actually paid for."""
    account = _account(db_session)
    offer = _offer(db_session)
    subscription = Subscription(
        subscriber_id=account.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(subscription)
    db_session.commit()

    invoice = _paid_prepaid_invoice(db_session, account, subscription)
    ensure_prepaid_entitlements_for_paid_invoice(db_session, invoice)
    db_session.commit()

    revoked = revoke_prepaid_entitlements_for_unpaid_invoice(db_session, invoice)

    assert revoked == []
    active = [
        e
        for e in _entitlements(db_session, invoice)
        if e.status == ServiceEntitlementStatus.active
    ]
    assert len(active) == 1


def test_refund_through_the_payment_owner_revokes_the_entitlement(db_session):
    """End-to-end: the revoke must be wired into the one place effects funnel through."""
    from app.services.billing.payments import _finalize_invoice_payment_effects

    account = _account(db_session)
    offer = _offer(db_session)
    subscription = Subscription(
        subscriber_id=account.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(subscription)
    db_session.commit()

    invoice = _paid_prepaid_invoice(db_session, account, subscription)
    ensure_prepaid_entitlements_for_paid_invoice(db_session, invoice)
    db_session.commit()

    # No payment backs the invoice any more (refunded / allocation removed), so the
    # canonical recompute will drop it out of `paid`.
    _finalize_invoice_payment_effects(db_session, invoice)
    db_session.commit()
    db_session.refresh(invoice)

    assert invoice.status != InvoiceStatus.paid
    active = [
        e
        for e in _entitlements(db_session, invoice)
        if e.status == ServiceEntitlementStatus.active
    ]
    assert not active, (
        "_finalize_invoice_payment_effects did not revoke the entitlement, so "
        "every refund path leaks free service"
    )


# --- L1: never claim a restore the owner refused ------------------------------


def test_no_caller_interpolates_a_restore_trigger():
    """``trigger`` is matched by EXACT membership against ALLOWED_RESTORERS.

    An interpolated trigger ("admin:<uuid>") silently matches nothing, resolves
    zero locks, and — because the False return was discarded — let the vacation-
    hold UI report "Service resumed successfully" while the customer stayed
    offline. The actor identity belongs in ``resolved_by``, which is free text.

    This is a whole failure class, not one bug, so guard the class.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    pattern = re.compile(r"trigger\s*=\s*f[\"']")
    for path in root.rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(root.parent)}:{lineno}")

    assert not offenders, (
        "restore trigger is an f-string; it is compared by exact membership "
        "against ALLOWED_RESTORERS and will silently resolve zero locks: "
        + ", ".join(offenders)
    )


def test_vacation_hold_resume_reports_the_owners_answer(db_session, monkeypatch):
    """The UI must not claim success the lifecycle owner did not give."""
    from app.services import web_catalog_subscription_workflows as workflows

    monkeypatch.setattr(
        workflows, "restore_subscription", lambda *a, **k: False, raising=False
    )
    redirect = workflows.admin_resume_vacation_hold_redirect(
        db_session, subscription_id=str(uuid.uuid4()), actor_id="tester"
    )
    assert "resumed successfully" not in redirect.lower(), (
        "the admin was told the service resumed when the owner declined"
    )
