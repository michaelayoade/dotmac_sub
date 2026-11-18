"""Real catalogue -> native cash/Tax issuance -> prepaid entitlement journey."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from dotmac_billing.contracts import GetCurrentInvoiceReceivableV1
from dotmac_billing.queries import get_current_invoice_receivable_v1
from sqlalchemy import func, select

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    Payment,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
    StarterInvoiceLink,
    TaxApplication,
)
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    OfferPrice,
    PriceType,
    SubscriptionStatus,
    UsageAllowance,
    UsageAllowanceResetBasis,
)
from app.models.customer_tax_policy import CustomerTaxPolicy
from app.models.usage import QuotaBucket
from app.schemas.billing import (
    ManualPaymentRecordingConfirm,
    ManualPaymentRecordingPreviewRequest,
)
from app.services import future_invoice_authoring as catalogue
from app.services import invoice_draft_authoring as drafts
from app.services import manual_payment_recording, settings_spec
from app.services.db_session_adapter import db_session_adapter
from app.services.invoice_withholding import (
    InvoiceWithholdingQuery,
    SetDraftInvoiceWithholdingSelection,
    set_draft_invoice_withholding_selection,
)
from app.services.invoice_withholding_issuance import (
    InvoiceWithholdingIssueConfirm,
    confirm_invoice_withholding_issue,
    preview_invoice_withholding_issue,
)
from tests.integration.test_native_invoice_withholding_flow import (
    _SCOPE,
    _context,
    _publish_fixture_policy,
)
from tests.integration.test_starter_manual_settlement_pg import (
    _seller_and_bank as _seller_and_bank,
)
from tests.integration.test_starter_manual_settlement_pg import (
    _starter_series_isolation as _starter_series_isolation,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("_starter_series_isolation", "_seller_and_bank"),
]


@pytest.mark.parametrize(
    ("capped", "withholding"), [(False, False), (False, True), (True, True)]
)
def test_prepaid_catalogue_native_settlement_activates_owner_period(
    db_session,
    subscriber,
    subscription,
    monkeypatch,
    capped,
    withholding,
):
    now = datetime.now(UTC)
    boundary = now + timedelta(days=10)
    subscriber.billing_mode = BillingMode.prepaid
    subscriber.billing_address_line1 = "2 Integration Customer Close"
    subscriber.billing_city = "Abuja"
    subscription.billing_mode = BillingMode.prepaid
    subscription.billing_cycle = BillingCycle.monthly
    subscription.status = SubscriptionStatus.active
    subscription.start_at = now - timedelta(days=20)
    subscription.next_billing_at = boundary
    subscription.unit_price = Decimal("100.00")
    if capped:
        allowance = UsageAllowance(
            name=f"Native quota {uuid4().hex[:8]}",
            included_gb=100,
            reset_basis=UsageAllowanceResetBasis.renewal_cycle,
            validity_days=30,
            is_active=True,
        )
        db_session.add(allowance)
        db_session.flush()
        subscription.offer.usage_allowance = allowance
    # Monthly future dates come from incumbent coverage, never test-invented
    # metadata. Capped quota may activate while this earlier coverage remains.
    db_session.add(
        ServiceEntitlement(
            account_id=subscriber.id,
            subscription_id=subscription.id,
            starts_at=now - timedelta(days=20),
            ends_at=boundary,
            amount_funded=Decimal("100.00"),
            currency="NGN",
            status=ServiceEntitlementStatus.active,
        )
    )
    price = OfferPrice(
        offer_id=subscription.offer_id,
        price_type=PriceType.recurring,
        amount=Decimal("100.00"),
        currency="NGN",
        billing_cycle=BillingCycle.monthly,
        tax_application=TaxApplication.exempt,
        is_active=True,
    )
    db_session.add_all(
        [
            price,
            CustomerTaxPolicy(
                account_id=subscriber.id,
                withholding_tax_enabled=True,
                version=1,
            ),
        ]
    )
    db_session.commit()
    jurisdiction_id, tax_code = _publish_fixture_policy(db_session)
    real_setting = settings_spec.resolve_value

    def configured_setting(db, domain, key):
        if key == "starter_billing_withholding_tax_jurisdiction_id":
            return str(jurisdiction_id)
        if key == "starter_billing_withholding_tax_code":
            return tax_code
        return real_setting(db, domain, key)

    monkeypatch.setattr(settings_spec, "resolve_value", configured_setting)
    choices = catalogue.future_period_choices(
        db_session,
        catalogue.FutureInvoicePeriodsQuery(subscriber.id, subscription.id, now),
    )
    line_preview = catalogue.preview_future_invoice_line(
        db_session,
        catalogue.PreviewFutureInvoiceLineQuery(
            subscriber.id,
            "NGN",
            catalogue.CatalogueItemKind.offer,
            subscription.offer_id,
            price.id,
            now,
            subscription_id=subscription.id,
            period_start=None if capped else choices[0].starts_at,
        ),
    )
    command = drafts.CreateInvoiceDraftCommand(
        account_id=subscriber.id,
        invoice_number=None,
        currency="NGN",
        issued_at=None,
        due_at=now + timedelta(days=14),
        memo=None,
        is_proforma=False,
        require_catalogue=True,
        lines=(
            drafts.DraftLineCommand(
                description="Server catalogue selection",
                quantity=Decimal("1"),
                unit_price=Decimal("100"),
                catalogue_selection=line_preview.selection,
            ),
        ),
    )
    db_session_adapter.release_read_transaction(db_session)
    draft = drafts.create_invoice_draft(
        db_session,
        command,
        context=_context("invoice_draft:test", key=f"draft-{uuid4()}"),
    )
    invoice_id = draft.invoice_id
    invoice = db_session.get(Invoice, invoice_id)
    line = next(line for line in invoice.lines if line.is_active)
    assert invoice.total == Decimal("100.00")
    assert invoice.invoice_number is None
    assert not invoice.withholding_tax_selected
    if capped:
        assert line_preview.starts_at is None and line_preview.ends_at is None
        assert "billing_period_start" not in line.metadata_
    else:
        assert line_preview.starts_at == choices[0].starts_at
    assert (
        db_session.scalar(
            select(ServiceEntitlement.id).where(
                ServiceEntitlement.source_invoice_id == invoice_id,
            )
        )
        is None
    )
    assert (
        db_session.scalar(
            select(QuotaBucket.id).where(
                QuotaBucket.subscription_id == subscription.id,
            )
        )
        is None
    )

    cash = Decimal("95.00") if withholding else Decimal("100.00")
    request = ManualPaymentRecordingPreviewRequest(
        account_id=subscriber.id,
        amount=cash,
        currency="NGN",
        status=PaymentStatus.succeeded,
        external_id=f"prepaid-bank-{uuid4()}",
        auto_allocate=False,
    )
    payment_preview = manual_payment_recording.preview_manual_payment_recording(
        db_session, request
    )
    key = f"prepaid-deposit-{uuid4()}"
    confirmation = ManualPaymentRecordingConfirm(
        **request.model_dump(),
        preview_fingerprint=payment_preview.payment_preview.fingerprint,
        control_fingerprint=payment_preview.control_fingerprint,
        idempotency_key=key,
    )
    db_session_adapter.release_read_transaction(db_session)
    manual_payment_recording.confirm_manual_payment_recording(
        db_session,
        context=_context("billing:payment:create", key=key),
        request=confirmation,
    )
    before_issue = datetime.now(UTC)
    db_session_adapter.release_read_transaction(db_session)
    if withholding:
        set_draft_invoice_withholding_selection(
            db_session,
            SetDraftInvoiceWithholdingSelection(invoice_id, True, 0),
            context=_context("billing:invoice:update"),
        )
        issue_preview = preview_invoice_withholding_issue(
            db_session, InvoiceWithholdingQuery(invoice_id)
        )
        assert issue_preview.snapshot.wht_amount == Decimal("5.00")
        issue = InvoiceWithholdingIssueConfirm(
            invoice_id,
            issue_preview.fingerprint,
            f"prepaid-declaration:{invoice_id}",
        )
        db_session_adapter.release_read_transaction(db_session)
        confirm_invoice_withholding_issue(
            db_session, issue, context=_context("billing:invoice:update")
        )
    else:
        drafts.issue_administrative_invoice(
            db_session,
            drafts.IssueAdministrativeInvoiceCommand(invoice_id),
            context=_context("billing:invoice:update"),
        )
    after_issue = datetime.now(UTC)
    db_session.expire_all()
    invoice = db_session.get(Invoice, invoice_id)
    assert invoice.status is InvoiceStatus.paid and invoice.balance_due == 0
    link = db_session.scalar(
        select(StarterInvoiceLink).where(StarterInvoiceLink.invoice_id == invoice_id)
    )
    funding = get_current_invoice_receivable_v1(
        db_session,
        scope=_SCOPE,
        request=GetCurrentInvoiceReceivableV1(_SCOPE, link.document_id),
    )
    assert funding.cash_applied.amount == cash
    assert funding.withholding_applied.amount == (
        Decimal("5.00") if withholding else Decimal("0.00")
    )
    assert funding.cash_and_withholding_settled and funding.outstanding.amount == 0
    entitlements = db_session.scalars(
        select(ServiceEntitlement).where(
            ServiceEntitlement.source_invoice_id == invoice_id,
        )
    ).all()
    assert len(entitlements) == 1
    entitlement = entitlements[0]
    if capped:
        assert before_issue <= funding.settlement_effective_at <= after_issue
        assert entitlement.starts_at == funding.settlement_effective_at
        assert entitlement.ends_at == funding.settlement_effective_at + timedelta(
            days=30
        )
        assert (
            invoice.billing_period_start is None and invoice.billing_period_end is None
        )
        bucket = db_session.scalar(
            select(QuotaBucket).where(QuotaBucket.subscription_id == subscription.id)
        )
        assert bucket is not None
        assert (
            bucket.period_start.replace(tzinfo=UTC) == funding.settlement_effective_at
        )
        line = next(line for line in invoice.lines if line.is_active)
        assert line.metadata_["capped_activation_withholding_ids"] == [
            str(value) for value in funding.withholding_discharge_ids
        ]
    else:
        assert entitlement.starts_at == line_preview.starts_at
        assert entitlement.ends_at == line_preview.ends_at
    for model in (Payment, LedgerEntry):
        assert (
            db_session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.account_id == subscriber.id)
            )
            == 0
        )
