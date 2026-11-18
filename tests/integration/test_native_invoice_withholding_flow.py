"""Real Sub command journey: one bank receipt funds selected and ordinary invoices.

Only deployment configuration is fixture-bound. Tax policy is actually published,
and Payments/Billing/selection/Issue commands execute against migrated PostgreSQL.
No mocked money result or manually constructed Tax seal qualifies this proof.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from dotmac_billing.contracts import (
    GetBillingAccountByExternalRefV1,
    GetCurrentInvoiceReceivableV1,
    GetCurrentReceivablePositionV1,
)
from dotmac_billing.queries import (
    get_billing_account_by_external_ref_v1,
    get_current_invoice_receivable_v1,
    get_current_receivable_position,
)
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency
from dotmac_tax.contracts import TaxAuthorityInput, TaxJurisdictionInput, TaxRuleInput
from dotmac_tax.service import (
    create_tax_authority,
    create_tax_code,
    create_tax_jurisdiction,
    publish_tax_rule,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    Payment,
    PaymentStatus,
    StarterInvoiceLink,
)
from app.models.catalog import BillingMode
from app.models.customer_tax_policy import CustomerTaxPolicy
from app.schemas.billing import (
    ManualPaymentRecordingConfirm,
    ManualPaymentRecordingPreviewRequest,
)
from app.services import (
    customer_tax_policies,
    invoice_draft_authoring,
    manual_payment_recording,
    settings_spec,
)
from app.services.billing import starter_policies
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.invoice_withholding import (
    InvoiceWithholdingQuery,
    SetDraftInvoiceWithholdingSelection,
    query_invoice_withholding,
    set_draft_invoice_withholding_selection,
)
from app.services.invoice_withholding_issuance import (
    InvoiceWithholdingIssueConfirm,
    confirm_invoice_withholding_issue,
    preview_invoice_withholding_issue,
)
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import CommandContext
from tests.integration.test_starter_issuance_switch_pg import _draft
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
_SCOPE = TenantScope(OPERATOR_TENANT_ID)
_CURRENCY = Currency("NGN", 2)


def _context(scope: str, *, key: str | None = None) -> CommandContext:
    return CommandContext.system(
        actor="user:native-wht-integration-staff",
        scope=scope,
        reason="Verify actual bank cash and invoice withholding command composition.",
        idempotency_key=key,
    )


def _publish_fixture_policy(db: Session) -> tuple[UUID, str]:
    now = datetime.now(UTC)
    suffix = uuid4().hex[:10]
    authority = create_tax_authority(
        db,
        tenant_id=OPERATOR_TENANT_ID,
        command=TaxAuthorityInput(
            f"FIXTURE-{suffix}", "Explicit integration test authority"
        ),
    )
    jurisdiction = create_tax_jurisdiction(
        db,
        tenant_id=OPERATOR_TENANT_ID,
        command=TaxJurisdictionInput(
            authority.id,
            f"FIXTURE-{suffix}",
            "Explicit integration test jurisdiction",
            "NG",
            _CURRENCY,
        ),
    )
    code = create_tax_code(
        db,
        tenant_id=OPERATOR_TENANT_ID,
        jurisdiction_id=jurisdiction.id,
        code=f"FIXTURE-WHT-{suffix}",
        name="Explicit integration withholding policy",
        tax_kind_code="withholding",
    )
    publish_tax_rule(
        db,
        tenant_id=OPERATOR_TENANT_ID,
        command=TaxRuleInput(
            tax_code_id=code.id,
            version=1,
            effective_from=(now - timedelta(days=1)).date(),
            effective_to=None,
            priority=1,
            fact_kind="invoice",
            recognition_basis_code="invoice_issued",
            transaction_side="withholding",
            calculation_method="percentage",
            rate=Decimal("0.05"),
            fixed_amount=None,
            inclusive=False,
            recoverable_rate=Decimal("0"),
            treatment_code="standard_rated",
        ),
        published_at=now - timedelta(hours=1),
    )
    db.commit()
    return jurisdiction.id, code.code


def _issue_audits(db: Session, invoice_id: UUID) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.entity_type == "invoice",
                AuditEvent.entity_id == str(invoice_id),
                AuditEvent.action == "issue_invoice_system",
            )
        )
        or 0
    )


@pytest.mark.parametrize("auto_allocate", [False, True])
@pytest.mark.parametrize(
    ("deposit_amount", "expected_remaining"),
    [(Decimal("210.00"), Decimal("0.00")), (Decimal("300.00"), Decimal("90.00"))],
)
def test_one_bank_receipt_funds_wht_and_unchecked_invoices_without_legacy_cash(
    db_session,
    subscriber,
    monkeypatch,
    auto_allocate,
    deposit_amount,
    expected_remaining,
):
    subscriber.billing_mode = BillingMode.postpaid
    subscriber.billing_address_line1 = "2 Integration Customer Close"
    subscriber.billing_city = "Abuja"
    db_session.add(
        CustomerTaxPolicy(
            account_id=subscriber.id, withholding_tax_enabled=True, version=1
        )
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
    request = ManualPaymentRecordingPreviewRequest(
        account_id=subscriber.id,
        amount=deposit_amount,
        currency="NGN",
        status=PaymentStatus.succeeded,
        external_id=f"actual-fixture-bank-receipt-{uuid4()}",
        auto_allocate=auto_allocate,
    )
    preview = manual_payment_recording.preview_manual_payment_recording(
        db_session, request
    )
    key = f"native-fixture-deposit-{uuid4()}"
    confirmed_request = ManualPaymentRecordingConfirm(
        **request.model_dump(),
        preview_fingerprint=preview.payment_preview.fingerprint,
        control_fingerprint=preview.control_fingerprint,
        idempotency_key=key,
    )
    db_session_adapter.release_read_transaction(db_session)
    deposit = manual_payment_recording.confirm_manual_payment_recording(
        db_session,
        context=_context("billing:payment:create", key=key),
        request=confirmed_request,
    )
    confirmation_id = deposit.payment_result.payment.id
    db_session_adapter.release_read_transaction(db_session)
    replay = manual_payment_recording.confirm_manual_payment_recording(
        db_session,
        context=_context("billing:payment:create", key=key),
        request=confirmed_request,
    )
    assert replay.idempotent_replay
    assert replay.payment_result.payment.id == confirmation_id
    other_key = f"duplicate-native-fixture-deposit-{uuid4()}"
    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(DomainError):
        manual_payment_recording.confirm_manual_payment_recording(
            db_session,
            context=_context("billing:payment:create", key=other_key),
            request=confirmed_request.model_copy(update={"idempotency_key": other_key}),
        )
    policy = starter_policies.resolve_starter_billing_policies(db_session)
    native_account = get_billing_account_by_external_ref_v1(
        db_session,
        scope=_SCOPE,
        request=GetBillingAccountByExternalRefV1(
            _SCOPE, policy.account_ref(subscriber.id), _CURRENCY
        ),
    )
    assert native_account is not None
    position_request = GetCurrentReceivablePositionV1(
        _SCOPE, native_account.account_id, _CURRENCY
    )
    funded = get_current_receivable_position(
        db_session, scope=_SCOPE, request=position_request
    )
    assert funded.position is not None
    assert funded.position.available_credit.amount == deposit_amount

    # These incumbent draft fixtures carry real VAT snapshots and due provenance.
    # Authoring/catalogue behavior is independently covered by the period tests.
    selected = _draft(db_session, subscriber)
    ordinary = _draft(db_session, subscriber)
    selected_id, ordinary_id = selected.id, ordinary.id
    assert (
        not selected.withholding_tax_selected and not ordinary.withholding_tax_selected
    )
    db_session_adapter.release_read_transaction(db_session)
    selection = set_draft_invoice_withholding_selection(
        db_session,
        SetDraftInvoiceWithholdingSelection(selected_id, True, 0),
        context=_context("billing:invoice:update"),
    )
    assert selection.selected and selection.version == 1
    preview = preview_invoice_withholding_issue(
        db_session, InvoiceWithholdingQuery(selected_id)
    )
    assert preview.snapshot.wht_amount == Decimal("5.00")
    assert preview.funding.cash_required.amount == Decimal("102.50")
    issue = InvoiceWithholdingIssueConfirm(
        selected_id, preview.fingerprint, f"fixture-declaration:{selected_id}"
    )
    db_session_adapter.release_read_transaction(db_session)
    assert (
        confirm_invoice_withholding_issue(
            db_session, issue, context=_context("billing:invoice:update")
        )
        == selected_id
    )
    before_replay = _issue_audits(db_session, selected_id)
    db_session_adapter.release_read_transaction(db_session)
    assert (
        confirm_invoice_withholding_issue(
            db_session, issue, context=_context("billing:invoice:update")
        )
        == selected_id
    )
    assert _issue_audits(db_session, selected_id) == before_replay == 1
    summary = query_invoice_withholding(
        db_session, InvoiceWithholdingQuery(selected_id)
    )
    assert summary.accepted_cash_amount == Decimal("102.50")
    assert summary.accepted_wht_amount == Decimal("5.00")
    assert summary.balance_due == 0
    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(DomainError) as frozen:
        set_draft_invoice_withholding_selection(
            db_session,
            SetDraftInvoiceWithholdingSelection(selected_id, False, 1),
            context=_context("billing:invoice:update"),
        )
    assert frozen.value.code.endswith("selection_immutable")
    db_session_adapter.release_read_transaction(db_session)
    # A later application session must read the unused deposit from SQL and
    # consume only the next invoice's cash. The fixture connection retains
    # its outer isolation transaction; no state is copied between ORM sessions.
    with Session(
        bind=db_session.connection(),
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    ) as later_session:
        carried = get_current_receivable_position(
            later_session, scope=_SCOPE, request=position_request
        )
        assert carried.position.available_credit.amount == deposit_amount - Decimal(
            "102.50"
        )
        db_session_adapter.release_read_transaction(later_session)
        result = invoice_draft_authoring.issue_administrative_invoice(
            later_session,
            invoice_draft_authoring.IssueAdministrativeInvoiceCommand(ordinary_id),
            context=_context("billing:invoice:update"),
        )
    db_session.expire_all()
    assert result == ordinary_id
    for invoice_id, cash, withholding in (
        (selected_id, Decimal("102.50"), Decimal("5.00")),
        (ordinary_id, Decimal("107.50"), Decimal("0.00")),
    ):
        invoice = db_session.get(Invoice, invoice_id)
        assert invoice.status == InvoiceStatus.paid and invoice.balance_due == 0
        link = db_session.scalar(
            select(StarterInvoiceLink).where(
                StarterInvoiceLink.invoice_id == invoice_id
            )
        )
        current = get_current_invoice_receivable_v1(
            db_session,
            scope=_SCOPE,
            request=GetCurrentInvoiceReceivableV1(_SCOPE, link.document_id),
        )
        assert current.cash_applied.amount == cash
        assert current.withholding_applied.amount == withholding
        assert current.outstanding.amount == 0
    remaining = get_current_receivable_position(
        db_session, scope=_SCOPE, request=position_request
    )
    assert (
        remaining.position is not None
        and remaining.position.available_credit.amount == expected_remaining
    )
    # Tax eligibility can change; an established native cash account must not
    # silently start creating legacy cash records for subsequent receipts.
    db_session_adapter.release_read_transaction(db_session)
    customer_tax_policies.set_customer_withholding_tax_policy(
        db_session,
        customer_tax_policies.SetCustomerWithholdingTaxPolicyCommand(
            request.account_id, False, "user:native-wht-integration-staff"
        ),
        context=_context(customer_tax_policies.WRITE_SCOPE),
    )
    followup = request.model_copy(
        update={
            "amount": Decimal("10.00"),
            "external_id": f"followup-bank-receipt-{uuid4()}",
        }
    )
    followup_preview = manual_payment_recording.preview_manual_payment_recording(
        db_session, followup
    )
    followup_key = f"followup-native-deposit-{uuid4()}"
    db_session_adapter.release_read_transaction(db_session)
    manual_payment_recording.confirm_manual_payment_recording(
        db_session,
        context=_context("billing:payment:create", key=followup_key),
        request=ManualPaymentRecordingConfirm(
            **followup.model_dump(),
            preview_fingerprint=followup_preview.payment_preview.fingerprint,
            control_fingerprint=followup_preview.control_fingerprint,
            idempotency_key=followup_key,
        ),
    )
    followup_position = get_current_receivable_position(
        db_session, scope=_SCOPE, request=position_request
    )
    assert (
        followup_position.position.available_credit.amount
        == expected_remaining + Decimal("10.00")
    )
    assert query_invoice_withholding(
        db_session, InvoiceWithholdingQuery(selected_id)
    ).accepted_wht_amount == Decimal("5.00")
    for legacy_model in (Payment, LedgerEntry):
        assert (
            db_session.scalar(
                select(func.count())
                .select_from(legacy_model)
                .where(legacy_model.account_id == subscriber.id)
            )
            == 0
        )
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.entity_id == str(confirmation_id),
                AuditEvent.action == "confirm_manual_payment_recording",
            )
        )
        == 1
    )
