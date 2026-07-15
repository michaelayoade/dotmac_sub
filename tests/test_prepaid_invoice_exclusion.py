"""Prepaid subscriptions must only enter invoice generation by explicit opt-in.

Production prepaid is monthly invoice-in-advance. Generic postpaid invoice paths
still exclude prepaid unless they pass ``allow_prepaid=True``.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    OfferPrice,
    PriceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import AccountStatus
from app.services import billing_automation
from app.services.billing.invoices import Invoices
from app.services.billing_automation import (
    generate_prorated_invoice,
    subscription_invoice_eligible,
)


def _add_recurring_price(db_session, offer_id, amount="100.00"):
    price = OfferPrice(
        offer_id=offer_id,
        price_type=PriceType.recurring,
        amount=Decimal(amount),
        currency="USD",
        billing_cycle=BillingCycle.monthly,
        is_active=True,
    )
    db_session.add(price)
    db_session.commit()
    return price


def _activate(db_session, subscription, subscriber_account, mode):
    now_naive = datetime.now(UTC).replace(tzinfo=None)
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = mode
    subscriber_account.status = AccountStatus.active
    subscription.start_at = now_naive - timedelta(days=30)
    subscription.next_billing_at = now_naive - timedelta(days=1)
    db_session.commit()
    return now_naive


def test_subscription_invoice_eligible_helper(db_session, subscription):
    subscription.billing_mode = BillingMode.prepaid
    assert subscription_invoice_eligible(subscription) is False
    assert subscription_invoice_eligible(subscription, allow_prepaid=True) is True
    subscription.billing_mode = BillingMode.postpaid
    assert subscription_invoice_eligible(subscription) is True


def test_invoice_cycle_skips_prepaid(db_session, subscription, subscriber_account):
    now_naive = _activate(
        db_session, subscription, subscriber_account, BillingMode.prepaid
    )
    _add_recurring_price(db_session, subscription.offer_id)

    summary = billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

    invoices = (
        db_session.query(Invoice)
        .filter(Invoice.account_id == subscriber_account.id)
        .count()
    )
    assert invoices == 0
    assert summary["prepaid_skipped"] >= 1


def test_invoice_cycle_prepaid_skipped_excludes_enabled_monthly_prepaid(
    db_session, subscription, subscriber_account, catalog_offer
):
    from app.models.domain_settings import DomainSetting, SettingDomain

    now_naive = _activate(
        db_session, subscription, subscriber_account, BillingMode.prepaid
    )
    catalog_offer.billing_cycle = BillingCycle.monthly
    _add_recurring_price(db_session, subscription.offer_id)
    db_session.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="prepaid_monthly_invoicing_enabled",
            value_text="true",
            value_json=True,
            is_active=True,
        )
    )
    db_session.commit()

    summary = billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

    assert summary["invoices_created"] == 1
    assert summary["prepaid_skipped"] == 0


def test_invoice_cycle_keeps_prepaid_and_postpaid_invoices_separate(
    db_session, subscription, subscriber_account, catalog_offer
):
    from app.models.domain_settings import DomainSetting, SettingDomain

    run_at = _activate(
        db_session, subscription, subscriber_account, BillingMode.prepaid
    )
    catalog_offer.billing_cycle = BillingCycle.monthly
    _add_recurring_price(db_session, subscription.offer_id)
    postpaid_subscription = Subscription(
        subscriber_id=subscriber_account.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.postpaid,
        start_at=run_at - timedelta(days=30),
        next_billing_at=run_at - timedelta(days=1),
    )
    db_session.add_all(
        [
            postpaid_subscription,
            DomainSetting(
                domain=SettingDomain.billing,
                key="prepaid_monthly_invoicing_enabled",
                value_text="true",
                value_json=True,
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    summary = billing_automation.run_invoice_cycle(db_session, run_at=run_at)

    assert summary["invoices_created"] == 2
    rows = (
        db_session.query(Subscription.billing_mode, Invoice.status)
        .join(InvoiceLine, InvoiceLine.subscription_id == Subscription.id)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .filter(Invoice.account_id == subscriber_account.id)
        .all()
    )
    assert set(rows) == {
        (BillingMode.prepaid, InvoiceStatus.draft),
        (BillingMode.postpaid, InvoiceStatus.issued),
    }


def test_overdue_runner_ignores_prepaid_subscription_invoice(
    db_session, subscription, subscriber_account, monkeypatch
):
    now_naive = _activate(
        db_session, subscription, subscriber_account, BillingMode.prepaid
    )
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-PREPAID-OVERDUE-IGNORE",
        status=InvoiceStatus.issued,
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        due_at=now_naive - timedelta(days=2),
        metadata_={},
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Prepaid renewal",
            quantity=Decimal("1.000"),
            unit_price=Decimal("100.00"),
            amount=Decimal("100.00"),
            metadata_={"kind": "base_subscription"},
        )
    )
    db_session.commit()
    emitted = []
    monkeypatch.setattr(
        billing_automation,
        "emit_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )

    result = billing_automation.mark_overdue_invoices(db_session)

    db_session.refresh(invoice)
    assert result["scanned"] == 1
    assert result["marked_overdue"] == 0
    assert invoice.status == InvoiceStatus.issued
    assert emitted == []


def test_overdue_runner_ignores_imported_line_less_prepaid_invoice(
    db_session, subscription, subscriber_account, monkeypatch
):
    now_naive = _activate(
        db_session, subscription, subscriber_account, BillingMode.prepaid
    )
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-IMPORTED-PREPAID-OVERDUE-IGNORE",
        status=InvoiceStatus.issued,
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        due_at=now_naive - timedelta(days=2),
        metadata_={"imported_via": "system_import_wizard"},
    )
    db_session.add(invoice)
    db_session.commit()
    emitted = []
    monkeypatch.setattr(
        billing_automation,
        "emit_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )

    result = billing_automation.mark_overdue_invoices(db_session)

    db_session.refresh(invoice)
    assert result["scanned"] == 1
    assert result["marked_overdue"] == 0
    assert invoice.status == InvoiceStatus.issued
    assert emitted == []


def test_overdue_runner_keeps_unclassified_imported_line_less_mixed_mode_invoice_collectible(
    db_session, subscription, subscriber_account, catalog_offer, monkeypatch
):
    run_at = _activate(
        db_session, subscription, subscriber_account, BillingMode.prepaid
    )
    subscriber_account.billing_mode = BillingMode.prepaid
    postpaid_subscription = Subscription(
        subscriber_id=subscriber_account.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.postpaid,
        start_at=run_at - timedelta(days=30),
        next_billing_at=run_at - timedelta(days=1),
    )
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-IMPORTED-MIXED-POSTPAID-COLLECTIBLE",
        status=InvoiceStatus.issued,
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        due_at=run_at - timedelta(days=2),
        metadata_={"imported_via": "system_import_wizard"},
    )
    db_session.add_all([postpaid_subscription, invoice])
    db_session.commit()
    monkeypatch.setattr(billing_automation, "emit_event", lambda *args, **kwargs: None)

    result = billing_automation.mark_overdue_invoices(db_session)

    db_session.refresh(invoice)
    assert result["marked_overdue"] == 1
    assert invoice.status == InvoiceStatus.overdue


def test_overdue_runner_ignores_explicit_prepaid_imported_line_less_mixed_mode_invoice(
    db_session, subscription, subscriber_account, catalog_offer, monkeypatch
):
    run_at = _activate(
        db_session, subscription, subscriber_account, BillingMode.prepaid
    )
    subscriber_account.billing_mode = BillingMode.prepaid
    postpaid_subscription = Subscription(
        subscriber_id=subscriber_account.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.postpaid,
        start_at=run_at - timedelta(days=30),
        next_billing_at=run_at - timedelta(days=1),
    )
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-IMPORTED-MIXED-PREPAID-NON-AR",
        status=InvoiceStatus.issued,
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        due_at=run_at - timedelta(days=2),
        metadata_={
            "imported_via": "system_import_wizard",
            "billing_mode": BillingMode.prepaid.value,
        },
    )
    db_session.add_all([postpaid_subscription, invoice])
    db_session.commit()
    emitted = []
    monkeypatch.setattr(
        billing_automation,
        "emit_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )

    result = billing_automation.mark_overdue_invoices(db_session)

    db_session.refresh(invoice)
    assert result["marked_overdue"] == 0
    assert invoice.status == InvoiceStatus.issued
    assert emitted == []


def test_overdue_runner_keeps_ambiguous_line_less_prepaid_invoice_visible(
    db_session, subscription, subscriber_account, monkeypatch
):
    now_naive = _activate(
        db_session, subscription, subscriber_account, BillingMode.prepaid
    )
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-MANUAL-PREPAID-STILL-COLLECTIBLE",
        status=InvoiceStatus.issued,
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        due_at=now_naive - timedelta(days=2),
        metadata_={},
    )
    db_session.add(invoice)
    db_session.commit()
    monkeypatch.setattr(billing_automation, "emit_event", lambda *args, **kwargs: None)

    result = billing_automation.mark_overdue_invoices(db_session)

    db_session.refresh(invoice)
    assert result["scanned"] == 1
    assert result["marked_overdue"] == 1
    assert invoice.status == InvoiceStatus.overdue


def test_billing_notifications_do_not_emit_overdue_events(
    db_session, subscription, subscriber_account, monkeypatch
):
    run_at = _activate(
        db_session, subscription, subscriber_account, BillingMode.prepaid
    )
    subscriber_account.billing_mode = BillingMode.prepaid
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-IMPORTED-PREPAID-NOTIFY-IGNORE",
        status=InvoiceStatus.overdue,
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        due_at=run_at - timedelta(days=3),
        metadata_={"imported_via": "system_import_wizard"},
    )
    db_session.add(invoice)
    db_session.commit()
    emitted = []
    monkeypatch.setattr(
        billing_automation,
        "emit_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )
    monkeypatch.setattr(
        billing_automation.enforcement_window,
        "within_send_window",
        lambda db, run_at: True,
    )

    result = billing_automation.run_billing_notifications(db_session, run_at)

    assert "dunning_escalations_sent" not in result
    assert emitted == []


def test_invoice_cycle_bills_postpaid(db_session, subscription, subscriber_account):
    now_naive = _activate(
        db_session, subscription, subscriber_account, BillingMode.postpaid
    )
    _add_recurring_price(db_session, subscription.offer_id)

    summary = billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

    invoices = (
        db_session.query(Invoice)
        .filter(Invoice.account_id == subscriber_account.id)
        .count()
    )
    assert invoices >= 1
    assert summary["invoices_created"] >= 1


def test_proration_skips_prepaid(db_session, subscription, subscriber_account):
    _activate(db_session, subscription, subscriber_account, BillingMode.prepaid)
    _add_recurring_price(db_session, subscription.offer_id)
    assert generate_prorated_invoice(db_session, subscription) is None


def test_create_for_subscription_blocks_prepaid(
    db_session, subscription, subscriber_account
):
    _activate(db_session, subscription, subscriber_account, BillingMode.prepaid)
    _add_recurring_price(db_session, subscription.offer_id)

    with pytest.raises(HTTPException) as exc:
        Invoices.create_for_subscription(
            db_session, str(subscriber_account.id), str(subscription.id)
        )
    assert exc.value.status_code == 400

    # explicit override succeeds
    invoice = Invoices.create_for_subscription(
        db_session,
        str(subscriber_account.id),
        str(subscription.id),
        allow_prepaid=True,
    )
    assert invoice is not None
