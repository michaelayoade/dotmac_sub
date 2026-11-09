"""Catalogue authoring uses server facts and retains existing date authorities."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models.billing import TaxApplication, TaxRate
from app.models.catalog import AddOn, AddOnPrice, PriceType, Subscription
from app.models.subscriber import Subscriber
from app.services import future_invoice_authoring as authoring
from app.services import invoice_draft_authoring as drafts
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def _one_time(
    db: Session, subscriber: Subscriber, *, inclusive: bool = False
) -> tuple[authoring.PreviewFutureInvoiceLineQuery, AddOnPrice]:
    rate = TaxRate(
        name="Catalogue VAT", code="catalogue-vat", rate=Decimal("7.5"), is_active=True
    )
    item = AddOn(name="Replacement router", is_active=True)
    db.add_all([rate, item])
    db.flush()
    subscriber.tax_rate_id = rate.id
    price = AddOnPrice(
        add_on_id=item.id,
        price_type=PriceType.one_time,
        amount=Decimal("107.50") if inclusive else Decimal("100.00"),
        currency="NGN",
        tax_application=TaxApplication.inclusive
        if inclusive
        else TaxApplication.exclusive,
        is_active=True,
    )
    db.add(price)
    db.commit()
    query = authoring.PreviewFutureInvoiceLineQuery(
        subscriber.id,
        "NGN",
        authoring.CatalogueItemKind.add_on,
        item.id,
        price.id,
        datetime.now(UTC),
        quantity=Decimal("2"),
    )
    return query, price


def test_one_time_catalogue_preview_calculates_quantity_and_inclusive_vat(
    db_session: Session, subscriber: Subscriber
) -> None:
    query, _ = _one_time(db_session, subscriber, inclusive=True)
    preview = authoring.preview_future_invoice_line(db_session, query)
    assert preview.quantity == 2
    assert preview.tax_total == Decimal("15.00")
    assert preview.total == Decimal("215.00")
    assert preview.starts_at is None
    assert preview.ends_at is None
    assert preview.period_kind is authoring.FutureInvoicePeriodKind.one_time
    assert (
        authoring.selection_from_wire(authoring.selection_to_wire(preview.selection))
        == preview.selection
    )


def test_price_change_invalidates_confirmation_even_if_browser_keeps_old_values(
    db_session: Session, subscriber: Subscriber
) -> None:
    query, price = _one_time(db_session, subscriber)
    preview = authoring.preview_future_invoice_line(db_session, query)
    price.amount = Decimal("120.00")
    db_session.flush()
    with pytest.raises(
        authoring.FutureInvoiceAuthoringError,
        match="Price, VAT or service dates changed",
    ):
        authoring.confirm_future_invoice_line(
            db_session,
            account_id=subscriber.id,
            currency="NGN",
            selection=preview.selection,
        )


def test_one_time_item_rejects_service_period_and_nonfinite_quantity(
    db_session: Session, subscriber: Subscriber
) -> None:
    query, _ = _one_time(db_session, subscriber)
    with pytest.raises(authoring.FutureInvoiceAuthoringError):
        authoring.preview_future_invoice_line(
            db_session,
            replace(query, period_start=datetime.now(UTC) + timedelta(days=30)),
        )
    with pytest.raises(authoring.FutureInvoiceAuthoringError):
        authoring.preview_future_invoice_line(
            db_session, replace(query, quantity=Decimal("NaN"))
        )


def test_draft_owner_ignores_browser_price_and_persists_catalogue_tax_and_source(
    db_session: Session, subscriber: Subscriber
) -> None:
    query, _ = _one_time(db_session, subscriber, inclusive=True)
    preview = authoring.preview_future_invoice_line(db_session, query)
    command = drafts.CreateInvoiceDraftCommand(
        account_id=subscriber.id,
        invoice_number=None,
        currency="NGN",
        issued_at=None,
        due_at=None,
        memo=None,
        is_proforma=False,
        lines=(
            drafts.DraftLineCommand(
                description="forged",
                quantity=Decimal("999"),
                unit_price=Decimal("0.01"),
                catalogue_selection=preview.selection,
            ),
        ),
        require_catalogue=True,
    )
    db_session_adapter.release_read_transaction(db_session)
    result = drafts.create_invoice_draft(
        db_session,
        command,
        context=CommandContext.system(
            actor="catalogue-test",
            scope="invoice_draft:test",
            reason="Verify server catalogue facts",
            idempotency_key="catalogue-facts",
        ),
    )
    from app.models.billing import Invoice

    invoice = db_session.get(Invoice, result.invoice_id)
    assert invoice.total == Decimal("215.00")
    line = next(line for line in invoice.lines if line.is_active)
    assert line.description == "Replacement router"
    assert line.quantity == 2
    assert line.unit_price == Decimal("107.50")
    assert line.tax_application is TaxApplication.inclusive
    assert line.metadata_["future_invoice"]["period_kind"] == "one_time"


def test_new_catalogue_draft_rejects_unselected_free_entry(
    db_session: Session, subscriber: Subscriber
) -> None:
    command = drafts.CreateInvoiceDraftCommand(
        account_id=subscriber.id,
        invoice_number=None,
        currency="NGN",
        issued_at=None,
        due_at=None,
        memo=None,
        is_proforma=False,
        lines=(drafts.DraftLineCommand("Manual", Decimal("1"), Decimal("100")),),
        require_catalogue=True,
    )
    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(
        drafts.InvoiceDraftAuthoringError, match="selected from the catalogue"
    ):
        drafts.create_invoice_draft(
            db_session,
            command,
            context=CommandContext.system(
                actor="catalogue-test",
                scope="invoice_draft:test",
                reason="Refuse free entry",
                idempotency_key="catalogue-refuse",
            ),
        )


def test_monthly_candidates_use_coverage_owner_boundary_not_signup_anniversary(
    db_session: Session,
    subscriber: Subscriber,
    subscription: Subscription,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.catalog import BillingCycle, BillingMode, SubscriptionStatus
    from app.services.prepaid_service_renewals import (
        PrepaidSettlementPeriod,
        PrepaidSubscriptionSettlementPeriod,
    )

    now = datetime.now(UTC)
    boundary = now + timedelta(days=9, hours=3)
    end = boundary + timedelta(days=31)
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.prepaid
    subscription.billing_cycle = BillingCycle.monthly
    subscription.start_at = now - timedelta(days=300)
    subscription.next_billing_at = now + timedelta(days=2)
    db_session.flush()
    observed = []

    def resolved(
        _db: Session, query: authoring.PrepaidSubscriptionSettlementPeriodQuery
    ) -> PrepaidSubscriptionSettlementPeriod:
        observed.append(query)
        return PrepaidSubscriptionSettlementPeriod(
            PrepaidSettlementPeriod(
                boundary, end, boundary.date(), end.date(), "Africa/Lagos"
            ),
            boundary,
        )

    monkeypatch.setattr(
        authoring, "resolve_prepaid_subscription_settlement_period", resolved
    )
    choices = authoring.future_period_choices(
        db_session,
        authoring.FutureInvoicePeriodsQuery(
            subscriber.id, subscription.id, now, horizon=2
        ),
    )
    assert observed[0].subscription_id == subscription.id
    assert choices[0].starts_at == boundary
    assert choices[0].ends_at == choices[1].starts_at
    assert choices[0].starts_at != subscription.next_billing_at


def test_lapsed_prepaid_does_not_promise_a_future_date_before_payment(
    db_session: Session,
    subscriber: Subscriber,
    subscription: Subscription,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.catalog import BillingCycle, BillingMode, SubscriptionStatus
    from app.services.prepaid_service_renewals import (
        PrepaidSettlementPeriod,
        PrepaidSubscriptionSettlementPeriod,
    )

    now = datetime.now(UTC)
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.prepaid
    subscription.billing_cycle = BillingCycle.monthly
    db_session.flush()
    monkeypatch.setattr(
        authoring,
        "resolve_prepaid_subscription_settlement_period",
        lambda db, query: PrepaidSubscriptionSettlementPeriod(
            PrepaidSettlementPeriod(
                now,
                now + timedelta(days=30),
                now.date(),
                (now + timedelta(days=30)).date(),
                "Africa/Lagos",
            ),
            None,
        ),
    )
    choices = authoring.future_period_choices(
        db_session,
        authoring.FutureInvoicePeriodsQuery(subscriber.id, subscription.id, now),
    )
    assert len(choices) == 1
    assert not choices[0].available
    assert choices[0].starts_at is None
    assert choices[0].ends_at is None


def test_paid_multiperiod_catalogue_lines_keep_exact_dates_and_replay_once(
    db_session: Session, subscriber: Subscriber, subscription: Subscription
) -> None:
    from sqlalchemy import select

    from app.models.billing import (
        Invoice,
        InvoiceLine,
        InvoiceStatus,
        ServiceEntitlement,
    )
    from app.models.catalog import BillingMode
    from app.services.service_entitlements import (
        ensure_prepaid_entitlements_for_paid_invoice,
    )

    subscription.billing_mode = BillingMode.prepaid
    starts_at = datetime.now(UTC) + timedelta(days=35, hours=2)
    middle = starts_at + timedelta(days=31)
    ends_at = middle + timedelta(days=30)
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("200"),
        total=Decimal("200"),
        balance_due=Decimal("0"),
        paid_at=datetime.now(UTC),
        is_active=True,
    )
    db_session.add(invoice)
    db_session.flush()
    for first, last in ((starts_at, middle), (middle, ends_at)):
        db_session.add(
            InvoiceLine(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                description="Selected future month",
                quantity=Decimal("1"),
                unit_price=Decimal("100"),
                amount=Decimal("100"),
                tax_application=TaxApplication.exempt,
                is_active=True,
                metadata_={
                    "kind": "base_subscription",
                    "future_catalogue_invoice": True,
                    "future_invoice": {"period_kind": "monthly"},
                    "billing_period_start": first.isoformat(),
                    "billing_period_end": last.isoformat(),
                },
            )
        )
    db_session.flush()
    created = ensure_prepaid_entitlements_for_paid_invoice(db_session, invoice)
    assert len(created) == 2
    assert {(row.starts_at, row.ends_at) for row in created} == {
        (starts_at, middle),
        (middle, ends_at),
    }
    assert ensure_prepaid_entitlements_for_paid_invoice(db_session, invoice) == []
    assert (
        len(
            db_session.scalars(
                select(ServiceEntitlement).where(
                    ServiceEntitlement.source_invoice_id == invoice.id
                )
            ).all()
        )
        == 2
    )
    assert invoice.billing_period_start is None
    assert invoice.billing_period_end is None


def test_issue_revalidation_rejects_changed_catalogue_price_without_repricing(
    db_session: Session, subscriber: Subscriber
) -> None:
    from app.models.billing import Invoice

    query, price = _one_time(db_session, subscriber, inclusive=True)
    preview = authoring.preview_future_invoice_line(db_session, query)
    command = drafts.CreateInvoiceDraftCommand(
        account_id=subscriber.id,
        invoice_number=None,
        currency="NGN",
        issued_at=None,
        due_at=None,
        memo=None,
        is_proforma=False,
        lines=(
            drafts.DraftLineCommand(
                preview.description,
                preview.quantity,
                preview.unit_price,
                catalogue_selection=preview.selection,
            ),
        ),
        require_catalogue=True,
    )
    db_session_adapter.release_read_transaction(db_session)
    result = drafts.create_invoice_draft(
        db_session,
        command,
        context=CommandContext.system(
            actor="catalogue-test",
            scope="invoice_draft:test",
            reason="Verify issue source validation",
            idempotency_key="catalogue-issue-source",
        ),
    )
    invoice = db_session.get(Invoice, result.invoice_id)
    assert invoice is not None
    original_total = invoice.total
    authoring.validate_catalogue_draft_for_issue(db_session, invoice)
    price = db_session.get(AddOnPrice, price.id)
    assert price is not None
    price.amount = Decimal("215")
    db_session.flush()
    with pytest.raises(authoring.FutureInvoiceAuthoringError, match="changed"):
        authoring.validate_catalogue_draft_for_issue(db_session, invoice)
    assert invoice.total == original_total


def test_capped_preview_names_quota_and_validity_without_prepromising_dates(
    db_session: Session, subscriber: Subscriber, subscription: Subscription
) -> None:
    from sqlalchemy import select

    from app.models.catalog import (
        BillingCycle,
        BillingMode,
        OfferPrice,
        SubscriptionStatus,
        UsageAllowance,
        UsageAllowanceResetBasis,
    )

    allowance = UsageAllowance(
        name="Quota 150GB",
        included_gb=150,
        reset_basis=UsageAllowanceResetBasis.renewal_cycle,
        validity_days=30,
        is_active=True,
    )
    db_session.add(allowance)
    db_session.flush()
    subscription.offer.usage_allowance = allowance
    subscription.billing_mode = BillingMode.prepaid
    subscription.billing_cycle = BillingCycle.monthly
    subscription.status = SubscriptionStatus.active
    subscription.unit_price = Decimal("100")
    price = db_session.scalar(
        select(OfferPrice).where(
            OfferPrice.offer_id == subscription.offer_id,
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
    )
    if price is None:
        price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("100"),
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            tax_application=TaxApplication.exempt,
            is_active=True,
        )
        db_session.add(price)
    db_session.commit()
    query = authoring.PreviewFutureInvoiceLineQuery(
        subscriber.id,
        "NGN",
        authoring.CatalogueItemKind.offer,
        subscription.offer_id,
        price.id,
        datetime.now(UTC),
        subscription_id=subscription.id,
    )
    preview = authoring.preview_future_invoice_line(db_session, query)
    assert (
        preview.period_kind is authoring.FutureInvoicePeriodKind.capped_pending_renewal
    )
    assert preview.allowance_gb == Decimal("150")
    assert preview.validity_days == 30
    assert "150" in preview.description and "30 days" in preview.description
    assert preview.starts_at is None and preview.ends_at is None
    assert "billing_period_start" not in authoring.preview_line_metadata(preview)


def test_catalogue_owner_accepts_one_time_and_distinct_subscription_periods(
    db_session: Session, subscriber: Subscriber, subscription: Subscription
) -> None:
    from sqlalchemy import select

    from app.models.billing import Invoice, ServiceEntitlement, ServiceEntitlementStatus
    from app.models.catalog import (
        BillingCycle,
        BillingMode,
        OfferPrice,
        SubscriptionStatus,
    )

    fee_query, _ = _one_time(db_session, subscriber)
    now = datetime.now(UTC)
    boundary = now + timedelta(days=10)
    subscription.billing_mode = BillingMode.prepaid
    subscription.billing_cycle = BillingCycle.monthly
    subscription.status = SubscriptionStatus.active
    subscription.unit_price = Decimal("100")
    other = Subscription(
        subscriber_id=subscriber.id,
        offer_id=subscription.offer_id,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        status=SubscriptionStatus.active,
        unit_price=Decimal("100"),
    )
    db_session.add(other)
    db_session.flush()
    for row in (subscription, other):
        db_session.add(
            ServiceEntitlement(
                account_id=subscriber.id,
                subscription_id=row.id,
                starts_at=now - timedelta(days=20),
                ends_at=boundary,
                amount_funded=Decimal("100"),
                currency="NGN",
                status=ServiceEntitlementStatus.active,
            )
        )
    db_session.commit()
    price = db_session.scalar(
        select(OfferPrice).where(
            OfferPrice.offer_id == subscription.offer_id,
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
    )
    if price is None:
        price = OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("100"),
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            tax_application=TaxApplication.exempt,
            is_active=True,
        )
        db_session.add(price)
        db_session.commit()
    first_choices = authoring.future_period_choices(
        db_session,
        authoring.FutureInvoicePeriodsQuery(subscriber.id, subscription.id, now),
    )
    other_choices = authoring.future_period_choices(
        db_session, authoring.FutureInvoicePeriodsQuery(subscriber.id, other.id, now)
    )
    selections = (
        authoring.preview_future_invoice_line(db_session, fee_query),
        authoring.preview_future_invoice_line(
            db_session,
            authoring.PreviewFutureInvoiceLineQuery(
                subscriber.id,
                "NGN",
                authoring.CatalogueItemKind.offer,
                subscription.offer_id,
                price.id,
                now,
                subscription_id=subscription.id,
                period_start=first_choices[0].starts_at,
            ),
        ),
        authoring.preview_future_invoice_line(
            db_session,
            authoring.PreviewFutureInvoiceLineQuery(
                subscriber.id,
                "NGN",
                authoring.CatalogueItemKind.offer,
                other.offer_id,
                price.id,
                now,
                subscription_id=other.id,
                period_start=other_choices[1].starts_at,
            ),
        ),
    )
    command = drafts.CreateInvoiceDraftCommand(
        account_id=subscriber.id,
        invoice_number=None,
        currency="NGN",
        issued_at=None,
        due_at=None,
        memo=None,
        is_proforma=False,
        lines=tuple(
            drafts.DraftLineCommand(
                row.description,
                row.quantity,
                row.unit_price,
                catalogue_selection=row.selection,
            )
            for row in selections
        ),
        require_catalogue=True,
    )
    db_session_adapter.release_read_transaction(db_session)
    created = drafts.create_invoice_draft(
        db_session,
        command,
        context=CommandContext.system(
            actor="catalogue-test",
            scope="invoice_draft:test",
            reason="Mixed catalogue document",
            idempotency_key="mixed-catalogue-document",
        ),
    )
    invoice = db_session.get(Invoice, created.invoice_id)
    assert invoice is not None
    lines = [line for line in invoice.lines if line.is_active]
    assert len(lines) == 3
    assert {line.subscription_id for line in lines} == {None, subscription.id, other.id}
    assert invoice.billing_period_start is None and invoice.billing_period_end is None


def test_future_service_rejects_account_subscription_billing_mode_mismatch(
    db_session: Session,
    subscriber: Subscriber,
    subscription: Subscription,
) -> None:
    from app.models.catalog import BillingMode, SubscriptionStatus

    subscriber.billing_mode = BillingMode.postpaid
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    db_session.flush()
    with pytest.raises(
        authoring.FutureInvoiceAuthoringError, match="different billing modes"
    ):
        authoring.future_period_choices(
            db_session,
            authoring.FutureInvoicePeriodsQuery(
                subscriber.id, subscription.id, datetime.now(UTC)
            ),
        )


def test_future_period_blocks_overlapping_recurring_addon_without_dropping_it(
    db_session: Session,
    subscriber: Subscriber,
    subscription: Subscription,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.catalog import (
        BillingCycle,
        BillingMode,
        SubscriptionAddOn,
        SubscriptionStatus,
    )
    from app.services.prepaid_service_renewals import (
        PrepaidSettlementPeriod,
        PrepaidSubscriptionSettlementPeriod,
    )

    now = datetime.now(UTC)
    boundary = now + timedelta(days=10)
    subscription.billing_mode = BillingMode.prepaid
    subscription.billing_cycle = BillingCycle.monthly
    subscription.status = SubscriptionStatus.active
    addon = AddOn(name="Recurring managed WiFi", is_active=True)
    db_session.add(addon)
    db_session.flush()
    db_session.add(
        AddOnPrice(
            add_on_id=addon.id,
            price_type=PriceType.recurring,
            amount=Decimal("20"),
            currency="NGN",
            is_active=True,
        )
    )
    db_session.add(
        SubscriptionAddOn(
            subscription_id=subscription.id,
            add_on_id=addon.id,
            quantity=1,
            start_at=boundary,
            end_at=boundary + timedelta(days=5),
        )
    )
    db_session.flush()

    def resolved(
        _db: Session, query: authoring.PrepaidSubscriptionSettlementPeriodQuery
    ) -> PrepaidSubscriptionSettlementPeriod:
        end = boundary + timedelta(days=31)
        return PrepaidSubscriptionSettlementPeriod(
            PrepaidSettlementPeriod(
                boundary, end, boundary.date(), end.date(), "Africa/Lagos"
            ),
            boundary,
        )

    monkeypatch.setattr(
        authoring, "resolve_prepaid_subscription_settlement_period", resolved
    )
    choices = authoring.future_period_choices(
        db_session,
        authoring.FutureInvoicePeriodsQuery(
            subscriber.id, subscription.id, now, horizon=2
        ),
    )
    assert not choices[0].available
    assert "recurring add-ons" in (choices[0].reason or "")
    assert choices[1].available


def test_confirmation_refreshes_cached_price_after_external_database_change(
    db_session: Session, subscriber: Subscriber
) -> None:
    from sqlalchemy import update

    query, price = _one_time(db_session, subscriber)
    preview = authoring.preview_future_invoice_line(db_session, query)
    db_session.execute(
        update(AddOnPrice)
        .where(AddOnPrice.id == price.id)
        .values(amount=Decimal("125"))
        .execution_options(synchronize_session=False)
    )
    assert price.amount == Decimal("100")
    with pytest.raises(authoring.FutureInvoiceAuthoringError, match="changed"):
        authoring.confirm_future_invoice_line(
            db_session,
            account_id=subscriber.id,
            currency="NGN",
            selection=preview.selection,
        )
    assert price.amount == Decimal("125")


def test_preview_refuses_to_discard_pending_catalogue_source_changes(
    db_session: Session, subscriber: Subscriber
) -> None:
    query, price = _one_time(db_session, subscriber)
    price.amount = Decimal("125")
    with pytest.raises(
        authoring.FutureInvoiceAuthoringError, match="pending catalogue"
    ):
        authoring.preview_future_invoice_line(db_session, query)
    assert price.amount == Decimal("125")
    assert db_session.is_modified(price)
