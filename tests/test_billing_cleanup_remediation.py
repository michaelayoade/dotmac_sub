from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import (
    AccessCredential,
    AccessType,
    AddOn,
    AddOnPrice,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.billing_cleanup_remediation import (
    apply_cleanup_remediation,
    discover_invoice_anchor_rows,
    plan_account_mode_row,
    plan_anchor_row,
    plan_cleanup_remediation,
    plan_disabled_service_line_row,
    plan_invoice_anchor_row,
    plan_missing_radius_row,
    plan_orphan_addon_row,
    plan_prepaid_collectible_ar_row,
    plan_prepaid_overlap_row,
    plan_stale_overdue_lock_row,
)


def _account(db, *, mode=BillingMode.prepaid):
    account = Subscriber(
        first_name="Cleanup",
        last_name="Target",
        email=f"{uuid.uuid4().hex}@example.com",
        status=SubscriberStatus.active,
        billing_mode=mode,
        is_active=True,
    )
    db.add(account)
    db.flush()
    return account


def _offer(db, *, mode=BillingMode.prepaid):
    offer = CatalogOffer(
        name=f"Cleanup {mode.value}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=mode,
        is_active=True,
    )
    db.add(offer)
    db.flush()
    return offer


def _subscription(
    db,
    account,
    *,
    mode=BillingMode.prepaid,
    status=SubscriptionStatus.active,
    next_billing_at=None,
):
    subscription = Subscription(
        subscriber_id=account.id,
        offer_id=_offer(db, mode=mode).id,
        status=status,
        billing_mode=mode,
        next_billing_at=next_billing_at,
    )
    db.add(subscription)
    db.flush()
    return subscription


def _invoice(
    db,
    account,
    *,
    status=InvoiceStatus.issued,
    amount="100.00",
    start=None,
    end=None,
):
    amount_decimal = Decimal(amount)
    invoice = Invoice(
        account_id=account.id,
        invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
        status=status,
        currency="NGN",
        subtotal=amount_decimal,
        tax_total=Decimal("0.00"),
        total=amount_decimal,
        balance_due=amount_decimal,
        billing_period_start=start,
        billing_period_end=end,
        due_at=start,
        is_active=True,
    )
    db.add(invoice)
    db.flush()
    return invoice


def _line(db, invoice, subscription, *, amount="100.00", description="Cleanup line"):
    line = InvoiceLine(
        invoice_id=invoice.id,
        subscription_id=subscription.id,
        description=description,
        quantity=Decimal("1.000"),
        unit_price=Decimal(amount),
        amount=Decimal(amount),
        is_active=True,
    )
    db.add(line)
    db.flush()
    return line


def test_resolves_stale_overdue_lock_and_restores_subscription(db_session):
    account = _account(db_session, mode=BillingMode.postpaid)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.postpaid,
        status=SubscriptionStatus.suspended,
    )
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=account.id,
        reason=EnforcementReason.overdue,
        source="invoice:test",
        is_active=True,
    )
    db_session.add(lock)
    db_session.commit()

    row = {
        "lock_id": str(lock.id),
        "account_id": str(account.id),
        "subscription_id": str(subscription.id),
        "source": "invoice:test",
    }
    item = plan_stale_overdue_lock_row(db_session, row)
    assert item["decision"] == "apply"
    assert item["would_restore"] is True

    result = apply_cleanup_remediation(db_session, {"items": [item]}, dry_run=False)

    db_session.refresh(lock)
    db_session.refresh(subscription)
    assert result["applied_count"] == 1
    assert lock.is_active is False
    assert subscription.status == SubscriptionStatus.active


def test_stale_lock_plan_refuses_when_account_still_has_overdue_ar(db_session):
    subscriber = _account(db_session, mode=BillingMode.postpaid)
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-COLLECTIBLE-AR",
        status=InvoiceStatus.overdue,
        currency="NGN",
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        due_at=datetime(2026, 7, 1, tzinfo=UTC),
        is_active=True,
    )
    db_session.add(invoice)
    db_session.flush()
    subscription = _subscription(
        db_session,
        subscriber,
        mode=BillingMode.postpaid,
        status=SubscriptionStatus.suspended,
    )
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=subscriber.id,
        reason=EnforcementReason.overdue,
        source=f"invoice:{invoice.id}",
        is_active=True,
    )
    db_session.add(lock)
    db_session.commit()

    item = plan_stale_overdue_lock_row(
        db_session,
        {
            "lock_id": str(lock.id),
            "account_id": str(subscriber.id),
            "subscription_id": str(subscription.id),
            "source": lock.source,
        },
    )

    assert item["decision"] == "refuse"
    assert item["reason"] == "account_has_collectible_overdue_ar"


def test_advances_prepaid_next_billing_anchor(db_session):
    account = _account(db_session, mode=BillingMode.prepaid)
    current = datetime(2026, 7, 1, tzinfo=UTC)
    target = current + timedelta(days=10)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.prepaid,
        next_billing_at=current,
    )
    db_session.commit()

    item = plan_anchor_row(
        db_session,
        {
            "account_id": str(account.id),
            "subscription_id": str(subscription.id),
            "current_next_billing_at": current.isoformat(),
            "paid_through": target.isoformat(),
        },
    )
    assert item["decision"] == "apply"

    apply_cleanup_remediation(db_session, {"items": [item]}, dry_run=False)

    db_session.refresh(subscription)
    assert subscription.next_billing_at.replace(tzinfo=UTC) == target


def test_anchor_plan_refuses_if_anchor_changed_since_audit(db_session):
    account = _account(db_session, mode=BillingMode.prepaid)
    current = datetime(2026, 7, 5, tzinfo=UTC)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.prepaid,
        next_billing_at=current,
    )
    db_session.commit()

    item = plan_anchor_row(
        db_session,
        {
            "account_id": str(account.id),
            "subscription_id": str(subscription.id),
            "current_next_billing_at": datetime(2026, 7, 1, tzinfo=UTC).isoformat(),
            "paid_through": datetime(2026, 7, 10, tzinfo=UTC).isoformat(),
        },
    )

    assert item["decision"] == "refuse"
    assert item["reason"] == "next_billing_at_changed_since_audit"


def test_discovers_and_repairs_invoice_backed_anchor(db_session):
    account = _account(db_session, mode=BillingMode.postpaid)
    current = datetime(2026, 7, 1, tzinfo=UTC)
    target = datetime(2026, 7, 10, tzinfo=UTC)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.postpaid,
        next_billing_at=current,
    )
    invoice = Invoice(
        account_id=account.id,
        invoice_number="INV-ANCHOR-1",
        status=InvoiceStatus.issued,
        currency="NGN",
        billing_period_start=datetime(2026, 6, 10, tzinfo=UTC),
        billing_period_end=target,
        is_active=True,
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Covered service",
            quantity=Decimal("1.000"),
            unit_price=Decimal("100.00"),
            amount=Decimal("100.00"),
            is_active=True,
        )
    )
    db_session.commit()

    rows = discover_invoice_anchor_rows(db_session)

    assert len(rows) == 1
    assert rows[0]["subscription_id"] == str(subscription.id)
    assert datetime.fromisoformat(rows[0]["paid_through"]).replace(tzinfo=UTC) == target
    item = plan_invoice_anchor_row(db_session, rows[0])
    assert item["decision"] == "apply"

    result = apply_cleanup_remediation(db_session, {"items": [item]}, dry_run=False)

    db_session.refresh(subscription)
    assert result["applied_count"] == 1
    assert subscription.next_billing_at.replace(tzinfo=UTC) == target
    assert discover_invoice_anchor_rows(db_session) == []


def test_invoice_anchor_discovery_ignores_draft_invoice_evidence(db_session):
    account = _account(db_session, mode=BillingMode.prepaid)
    current = datetime(2026, 7, 1, tzinfo=UTC)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.prepaid,
        next_billing_at=current,
    )
    invoice = Invoice(
        account_id=account.id,
        invoice_number="INV-DRAFT-ANCHOR",
        status=InvoiceStatus.draft,
        currency="NGN",
        billing_period_start=datetime(2026, 7, 1, tzinfo=UTC),
        billing_period_end=datetime(2026, 8, 1, tzinfo=UTC),
        is_active=True,
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Unfunded advance service",
            quantity=Decimal("1.000"),
            unit_price=Decimal("100.00"),
            amount=Decimal("100.00"),
            is_active=True,
        )
    )
    db_session.commit()

    assert discover_invoice_anchor_rows(db_session) == []


def test_invoice_anchor_plan_refuses_if_anchor_changed_since_audit(db_session):
    account = _account(db_session, mode=BillingMode.postpaid)
    current = datetime(2026, 7, 5, tzinfo=UTC)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.postpaid,
        next_billing_at=current,
    )
    db_session.commit()

    item = plan_invoice_anchor_row(
        db_session,
        {
            "issue": "invoice_anchor_behind_paid_through",
            "account_id": str(account.id),
            "subscription_id": str(subscription.id),
            "subscription_status": SubscriptionStatus.active.value,
            "subscription_mode": BillingMode.postpaid.value,
            "current_next_billing_at": datetime(2026, 7, 1, tzinfo=UTC).isoformat(),
            "paid_through": datetime(2026, 7, 10, tzinfo=UTC).isoformat(),
        },
    )

    assert item["decision"] == "refuse"
    assert item["reason"] == "next_billing_at_changed_since_audit"


def test_aligns_account_mode_when_single_live_subscription_mode(db_session):
    account = _account(db_session, mode=BillingMode.prepaid)
    subscription = _subscription(db_session, account, mode=BillingMode.postpaid)
    db_session.commit()

    item = plan_account_mode_row(
        db_session,
        {
            "issue": "subscription_vs_account",
            "subscriber_id": str(account.id),
            "subscription_id": str(subscription.id),
            "subscription_mode": "postpaid",
            "account_mode": "prepaid",
        },
    )
    assert item["decision"] == "apply"

    apply_cleanup_remediation(db_session, {"items": [item]}, dry_run=False)

    db_session.refresh(account)
    assert account.billing_mode == BillingMode.postpaid


def test_account_mode_plan_refuses_mixed_live_modes(db_session):
    account = _account(db_session, mode=BillingMode.prepaid)
    _subscription(db_session, account, mode=BillingMode.prepaid)
    _subscription(db_session, account, mode=BillingMode.postpaid)
    db_session.commit()

    item = plan_account_mode_row(
        db_session,
        {
            "issue": "subscription_vs_account",
            "subscriber_id": str(account.id),
            "subscription_mode": "postpaid",
            "account_mode": "prepaid",
        },
    )

    assert item["decision"] == "refuse"
    assert item["reason"] == "mixed_or_changed_live_subscription_modes"


def test_deactivates_disabled_service_line_and_voids_empty_invoice(db_session):
    account = _account(db_session, mode=BillingMode.postpaid)
    ended_at = datetime(2026, 6, 1, tzinfo=UTC)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.postpaid,
        status=SubscriptionStatus.canceled,
    )
    subscription.canceled_at = ended_at
    invoice = _invoice(
        db_session,
        account,
        start=datetime(2026, 7, 1, tzinfo=UTC),
        end=datetime(2026, 8, 1, tzinfo=UTC),
    )
    line = _line(db_session, invoice, subscription)
    db_session.commit()

    item = plan_disabled_service_line_row(
        db_session,
        {
            "finding_type": "disabled_service",
            "proposed_disposition": "credit_or_void_required",
            "invoice_id": str(invoice.id),
            "invoice_status": InvoiceStatus.issued.value,
            "invoice_line_id": str(line.id),
            "subscription_id": str(subscription.id),
        },
    )

    assert item["decision"] == "apply"
    result = apply_cleanup_remediation(db_session, {"items": [item]}, dry_run=False)

    db_session.refresh(line)
    db_session.refresh(invoice)
    assert result["applied_count"] == 1
    assert line.is_active is False
    assert invoice.status == InvoiceStatus.void
    assert invoice.total == Decimal("0.00")
    assert invoice.balance_due == Decimal("0.00")


def test_disabled_service_line_refuses_invoice_with_ledger_activity(db_session):
    account = _account(db_session, mode=BillingMode.postpaid)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.postpaid,
        status=SubscriptionStatus.canceled,
    )
    subscription.canceled_at = datetime(2026, 6, 1, tzinfo=UTC)
    invoice = _invoice(
        db_session,
        account,
        start=datetime(2026, 7, 1, tzinfo=UTC),
        end=datetime(2026, 8, 1, tzinfo=UTC),
    )
    line = _line(db_session, invoice, subscription)
    db_session.add(
        LedgerEntry(
            account_id=account.id,
            invoice_id=invoice.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.invoice,
            amount=Decimal("100.00"),
            currency="NGN",
            is_active=True,
        )
    )
    db_session.commit()

    item = plan_disabled_service_line_row(
        db_session,
        {
            "finding_type": "disabled_service",
            "proposed_disposition": "credit_or_void_required",
            "invoice_id": str(invoice.id),
            "invoice_status": InvoiceStatus.issued.value,
            "invoice_line_id": str(line.id),
            "subscription_id": str(subscription.id),
        },
    )

    assert item["decision"] == "refuse"
    assert item["reason"] == "invoice_has_financial_activity"


def test_duplicate_period_group_deactivates_newer_duplicate_line(db_session):
    account = _account(db_session, mode=BillingMode.postpaid)
    subscription = _subscription(db_session, account, mode=BillingMode.postpaid)
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = datetime(2026, 8, 1, tzinfo=UTC)
    invoice = _invoice(db_session, account, amount="200.00", start=start, end=end)
    keep = _line(db_session, invoice, subscription, description="Same period")
    duplicate = _line(db_session, invoice, subscription, description="Same period")
    db_session.commit()
    db_session.refresh(invoice)
    group_key = "|".join(
        [
            str(subscription.id),
            invoice.billing_period_start.isoformat(),
            invoice.billing_period_end.isoformat(),
            "Same period",
        ]
    )

    plan = plan_cleanup_remediation(
        db_session,
        duplicate_line_rows=[
            {
                "finding_type": "duplicate_period",
                "proposed_disposition": "duplicate_review",
                "invoice_status": InvoiceStatus.issued.value,
                "invoice_line_id": str(keep.id),
                "duplicate_group_key": group_key,
            },
            {
                "finding_type": "duplicate_period",
                "proposed_disposition": "duplicate_review",
                "invoice_status": InvoiceStatus.issued.value,
                "invoice_line_id": str(duplicate.id),
                "duplicate_group_key": group_key,
            },
        ],
    )

    assert plan["counts"]["apply"] == 1
    apply_cleanup_remediation(db_session, plan, dry_run=False)

    db_session.refresh(keep)
    db_session.refresh(duplicate)
    db_session.refresh(invoice)
    assert keep.is_active is True
    assert duplicate.is_active is False
    assert invoice.total == Decimal("100.00")
    assert invoice.balance_due == Decimal("100.00")


def test_orphan_recurring_addon_is_ended_at_parent_end(db_session):
    account = _account(db_session, mode=BillingMode.postpaid)
    parent_end = datetime(2026, 7, 1, tzinfo=UTC)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.postpaid,
        status=SubscriptionStatus.canceled,
    )
    subscription.canceled_at = parent_end
    addon = AddOn(name="Static IP", is_active=True)
    db_session.add(addon)
    db_session.flush()
    db_session.add(
        AddOnPrice(
            add_on_id=addon.id,
            price_type=PriceType.recurring,
            amount=Decimal("1000.00"),
            currency="NGN",
            is_active=True,
        )
    )
    sub_addon = SubscriptionAddOn(
        subscription_id=subscription.id,
        add_on_id=addon.id,
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
        end_at=None,
    )
    db_session.add(sub_addon)
    db_session.commit()

    item = plan_orphan_addon_row(
        db_session,
        {
            "subscription_add_on_id": str(sub_addon.id),
            "subscription_id": str(subscription.id),
            "current_end_at": "",
        },
    )

    assert item["decision"] == "apply"
    apply_cleanup_remediation(db_session, {"items": [item]}, dry_run=False)

    db_session.refresh(sub_addon)
    assert sub_addon.end_at.replace(tzinfo=UTC) == parent_end


def test_missing_radius_refuses_unusable_credential(db_session):
    account = _account(db_session, mode=BillingMode.prepaid)
    subscription = _subscription(db_session, account, mode=BillingMode.prepaid)
    subscription.login = "missing-user"
    db_session.add(
        AccessCredential(
            subscriber_id=account.id,
            username="missing-user",
            secret_hash="",
            is_active=True,
        )
    )
    db_session.commit()

    item = plan_missing_radius_row(
        db_session,
        {"subscription_id": str(subscription.id), "login": "missing-user"},
    )

    assert item["decision"] == "refuse"
    assert item["reason"] == "credential_unusable_requires_password_reset"


def test_missing_radius_syncs_when_credential_is_usable(db_session, monkeypatch):
    account = _account(db_session, mode=BillingMode.prepaid)
    subscription = _subscription(db_session, account, mode=BillingMode.prepaid)
    subscription.login = "sync-user"
    db_session.add(
        AccessCredential(
            subscriber_id=account.id,
            username="sync-user",
            secret_hash="plain:test123",
            is_active=True,
        )
    )
    db_session.commit()

    def fake_reconcile(db, subscription_id):
        assert subscription_id == str(subscription.id)
        return {"ok": True, "radius_users_changed": 1}

    monkeypatch.setattr(
        "app.services.radius.reconcile_subscription_connectivity",
        fake_reconcile,
    )
    item = plan_missing_radius_row(
        db_session,
        {"subscription_id": str(subscription.id), "login": "sync-user"},
    )

    assert item["decision"] == "apply"
    result = apply_cleanup_remediation(db_session, {"items": [item]}, dry_run=False)
    assert result["applied"][0]["after"]["radius_users_changed"] == 1


def test_prepaid_collectible_ar_drafts_unfunded_untouched_invoice(db_session):
    account = _account(db_session, mode=BillingMode.prepaid)
    _subscription(db_session, account, mode=BillingMode.prepaid)
    invoice = _invoice(db_session, account, status=InvoiceStatus.overdue)
    db_session.commit()

    item = plan_prepaid_collectible_ar_row(
        db_session,
        {"invoice_id": str(invoice.id), "invoice_status": InvoiceStatus.overdue.value},
    )

    assert item["decision"] == "apply"
    apply_cleanup_remediation(db_session, {"items": [item]}, dry_run=False)

    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.draft
    assert invoice.due_at is None
    assert "prepaid_phantom_ar_cleanup" in (invoice.metadata_ or {})


def test_prepaid_collectible_ar_refuses_partially_paid_invoice(db_session):
    account = _account(db_session, mode=BillingMode.prepaid)
    _subscription(db_session, account, mode=BillingMode.prepaid)
    invoice = _invoice(db_session, account, status=InvoiceStatus.partially_paid)
    db_session.commit()

    item = plan_prepaid_collectible_ar_row(
        db_session,
        {
            "invoice_id": str(invoice.id),
            "invoice_status": InvoiceStatus.partially_paid.value,
        },
    )

    assert item["decision"] == "refuse"
    assert item["reason"] == "partially_paid_requires_manual_review"


def test_prepaid_overlap_voids_only_safe_unpaid_candidate(db_session):
    account = _account(db_session, mode=BillingMode.prepaid)
    invoice = _invoice(db_session, account, status=InvoiceStatus.issued)
    db_session.commit()

    item = plan_prepaid_overlap_row(
        db_session,
        {
            "action": "void_unpaid_invoice",
            "bad_invoice_id": str(invoice.id),
            "bad_invoice_status": InvoiceStatus.issued.value,
            "valid_paid_invoice_id": str(uuid.uuid4()),
            "corrected_next_billing_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
        },
    )

    assert item["decision"] == "apply"
    apply_cleanup_remediation(db_session, {"items": [item]}, dry_run=False)

    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.void
    assert invoice.balance_due == Decimal("0.00")
    assert "prepaid_overlap_cleanup" in (invoice.metadata_ or {})


def test_prepaid_overlap_refuses_manual_review_candidate(db_session):
    item = plan_prepaid_overlap_row(
        db_session,
        {"action": "hold_for_manual_review", "bad_invoice_id": str(uuid.uuid4())},
    )

    assert item["decision"] == "refuse"
    assert item["reason"] == "overlap_requires_manual_review"


def test_plan_cleanup_remediation_combines_counts(db_session):
    account = _account(db_session, mode=BillingMode.prepaid)
    current = datetime(2026, 7, 1, tzinfo=UTC)
    subscription = _subscription(
        db_session,
        account,
        mode=BillingMode.prepaid,
        next_billing_at=current,
    )
    db_session.commit()

    plan = plan_cleanup_remediation(
        db_session,
        anchor_rows=[
            {
                "account_id": str(account.id),
                "subscription_id": str(subscription.id),
                "current_next_billing_at": current.isoformat(),
                "paid_through": datetime(2026, 7, 10, tzinfo=UTC).isoformat(),
            }
        ],
        mode_rows=[{"issue": "subscription_vs_offer"}],
    )

    assert plan["counts"]["apply"] == 1
    assert plan["counts"]["refuse"] == 1
