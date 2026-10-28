"""Native issuance and immutable Tax presentation on migrated PostgreSQL.

Pending Billing a10 dependency publication; no local execution is authorized.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

import pytest
from dotmac_billing import InvoiceDocumentFactV1, InvoiceTaxEvidenceV2
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money
from dotmac_payments import OpenPaymentIntent, PaymentPurpose, open_payment_intent
from dotmac_tax import (
    TaxAuthorityInput,
    TaxBaseDependencyPolicyV1,
    TaxFactSignatureV1,
    TaxJurisdictionInput,
    TaxRuleInputV2,
    ensure_tax_authority,
    ensure_tax_code,
    ensure_tax_jurisdiction,
    ensure_tax_rule_v2_in_transaction,
    get_tax_determination_set_v2,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus, StarterInvoiceLink
from app.models.catalog import BillingMode
from app.models.subscriber import Subscriber
from app.schemas.audit import AuditEventCreate
from app.services import direct_transfer_intents
from app.services.billing import starter_issuance, starter_positions
from app.services.billing.starter_settlement import settle_starter_invoice_payment
from app.services.domain_errors import DomainError
from app.services.operator_tenant import OPERATOR_TENANT_ID
from tests.integration.test_starter_issuance_switch_pg import (
    _draft,
    _seller_and_bank,  # noqa: F401 -- shared pytest fixture
    _starter_series_isolation,  # noqa: F401 -- shared pytest fixture
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("_seller_and_bank", "_starter_series_isolation"),
]


def _issue(
    db_session: Session, subscriber: Subscriber
) -> tuple[Invoice, StarterInvoiceLink]:
    subscriber.billing_mode = BillingMode.postpaid
    subscriber.billing_address_line1 = "2 Customer Close"
    subscriber.billing_city = "Abuja"
    invoice = _draft(db_session, subscriber)
    link = starter_issuance.issue_admin_invoice_via_starter(
        db_session, invoice=invoice, actor_ref="user:native-tax-proof"
    )
    db_session.flush()
    return invoice, link


def test_missing_policy_refuses_after_eligibility_without_legacy_fallback(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setattr(starter_issuance, "resolve_value", lambda *_args: None)
    with pytest.raises(
        starter_issuance.StarterIssuanceError, match="policy is missing"
    ):
        _issue(db_session, subscriber)
    assert db_session.scalar(select(StarterInvoiceLink)) is None


def test_issuance_freezes_tax_and_policy_edit_does_not_change_document(
    db_session, subscriber, starter_native_tax_policy
):
    binding = starter_native_tax_policy(withholding_rate=Decimal("0.05"))
    invoice, link = _issue(db_session, subscriber)
    fact = starter_positions.starter_invoice_fact(db_session, invoice)
    assert fact is not None
    assert len(fact.tax_evidence) == 2
    assert {item.purpose for item in fact.tax_evidence} == {
        "invoice_tax",
        "expected_withholding",
    }
    assert all(
        item.determination.source_fingerprint and item.determination.result_fingerprint
        for item in fact.tax_evidence
    )
    assert fact.expected_withholding.amount == Decimal("5.00")
    assert invoice.balance_due == Decimal("107.50")
    legacy = InvoiceDocumentFactV1(
        **{
            field.name: getattr(fact, field.name)
            for field in fields(InvoiceDocumentFactV1)
        }
    )
    assert (
        starter_positions.apply_fact_overlay(invoice, legacy).tax_presentation is None
    )
    snapshot = direct_transfer_intents._invoice_withholding_tax_snapshot(
        db_session, account_id=subscriber.id, invoice=invoice
    )
    assert snapshot["schema_version"] == 2
    assert snapshot["net_amount"] == "102.50"
    assert "withholding_tax_rate_percent" not in snapshot
    binding["invoice_tax"]["jurisdiction_id"] = str(uuid4())
    assert starter_positions.starter_invoice_fact(db_session, invoice) == fact

    now = datetime.now(UTC)
    scope = TenantScope(OPERATOR_TENANT_ID)
    reference = f"native-tax-payment:{uuid4()}"
    open_payment_intent(
        db_session,
        scope=scope,
        command=OpenPaymentIntent(
            reference=reference,
            payer_reference=str(subscriber.id),
            purpose=PaymentPurpose.INVOICE_SETTLEMENT,
            target_reference=str(invoice.id),
            requested=Money.of("107.50", Currency("NGN", 2)),
            provider_type="paystack",
            channel="customer_selfcare",
            opened_at=now,
            expires_at=now + timedelta(days=1),
        ),
    )
    db_session.flush()
    settle_starter_invoice_payment(
        db_session,
        invoice=invoice,
        link=link,
        intent_reference=reference,
        external_reference=f"native-tax-payment:{uuid4()}",
        confirmed_amount=Decimal("107.50"),
        occurred_at=now,
        observed_at=now,
    )
    db_session.flush()
    view = starter_positions.apply_fact_overlay(invoice, fact)
    assert invoice.status is InvoiceStatus.paid
    assert view.balance_due == 0
    assert view.tax_presentation.original_expected_net == Decimal("102.50")
    with pytest.raises(DomainError) as refusal:
        direct_transfer_intents._invoice_withholding_tax_snapshot(
            db_session, account_id=subscriber.id, invoice=invoice
        )
    assert refusal.value.code.endswith("invoice_withholding_tax_basis_unavailable")
    assert view.total == Decimal("107.50")


def _publish_dependency_policy(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    *,
    selection: Literal["all_prior", "tax_code_ids"] = "all_prior",
    vat_rate: Decimal = Decimal("0.075"),
    missing_selected_code: bool = False,
) -> dict[str, dict[str, object]]:
    """Author policies through public Tax APIs; assembly supplies only routing."""
    code = f"dependency-{uuid4().hex[:12]}"
    authority = ensure_tax_authority(
        db,
        tenant_id=OPERATOR_TENANT_ID,
        command=TaxAuthorityInput(code=code, name="Synthetic dependency authority"),
    )
    jurisdiction = ensure_tax_jurisdiction(
        db,
        tenant_id=OPERATOR_TENANT_ID,
        command=TaxJurisdictionInput(
            authority_id=authority.authority_id,
            code=code,
            name="Synthetic dependency jurisdiction",
            country_code="NG",
            currency=Currency("NGN", 2),
        ),
    )
    codes: dict[str, UUID] = {}
    for purpose in ("invoice_tax", "expected_withholding", "unselected"):
        codes[purpose] = ensure_tax_code(
            db,
            tenant_id=OPERATOR_TENANT_ID,
            jurisdiction_id=jurisdiction.jurisdiction_id,
            code=purpose,
            name=purpose,
            tax_kind_code=purpose,
        ).tax_code_id
    signature = TaxFactSignatureV1(
        jurisdiction.jurisdiction_id,
        f"{code}:invoice_tax",
        "invoice_expectation",
        "output",
    )
    dependency = TaxBaseDependencyPolicyV1(
        prior_signature=signature,
        selection=selection,
        tax_code_ids=(codes["unselected" if missing_selected_code else "invoice_tax"],)
        if selection == "tax_code_ids"
        else (),
    )
    binding: dict[str, dict[str, object]] = {}
    for purpose, side, rate in (
        ("invoice_tax", "output", vat_rate),
        ("expected_withholding", "withholding", Decimal("0.05")),
    ):
        ensure_tax_rule_v2_in_transaction(
            db,
            tenant_id=OPERATOR_TENANT_ID,
            command=TaxRuleInputV2(
                tax_code_id=codes[purpose],
                version=1,
                effective_from=date(2000, 1, 1),
                effective_to=None,
                priority=1,
                fact_kind=f"{code}:{purpose}",
                recognition_basis_code="invoice_expectation",
                transaction_side=side,
                calculation_method="percentage",
                rate=rate,
                fixed_amount=None,
                inclusive=False,
                recoverable_rate=Decimal("0"),
                treatment_code="exempt" if rate == 0 else "standard_rated",
                calculation_base_code="source_plus_dependency_tax"
                if purpose == "expected_withholding"
                else "source_amount",
                dependency_policy=dependency
                if purpose == "expected_withholding"
                else None,
            ),
        )
        binding[purpose] = {
            "jurisdiction_id": str(jurisdiction.jurisdiction_id),
            "fact_kind": f"{code}:{purpose}",
            "recognition_basis_code": "invoice_expectation",
            "transaction_side": side,
            "place_ref": "fixture:place",
            "prior_purposes": ["invoice_tax"]
            if purpose == "expected_withholding"
            else [],
        }
    real_resolve = starter_issuance.resolve_value
    monkeypatch.setattr(
        starter_issuance,
        "resolve_value",
        lambda db, domain, key: (
            binding
            if key == "starter_invoice_tax_policy"
            else real_resolve(db, domain, key)
        ),
    )
    db.flush()
    return binding


@pytest.mark.parametrize("selection", ("all_prior", "tax_code_ids"))
def test_vat_dependency_uses_original_source_and_freezes_selected_prior(
    db_session: Session,
    subscriber: Subscriber,
    monkeypatch: pytest.MonkeyPatch,
    selection: Literal["all_prior", "tax_code_ids"],
) -> None:
    binding = _publish_dependency_policy(db_session, monkeypatch, selection=selection)
    invoice, _link = _issue(db_session, subscriber)
    fact = starter_positions.starter_invoice_fact(db_session, invoice)
    assert fact is not None
    vat, withholding = fact.tax_evidence
    result = withholding.determination
    assert result.source_amount.amount == Decimal("100.00")
    assert result.components[0].base_amount.amount == Decimal("107.50")
    assert result.tax_amount.amount == Decimal("5.38")
    assert withholding.prior_determinations == (vat.determination,)
    assert result.commercial_source == vat.determination.commercial_source
    assert result.commercial_source.source_ref != result.source_ref
    assert fact.commercial_sources == (result.commercial_source,)
    owner_result = get_tax_determination_set_v2(
        db_session,
        tenant_id=OPERATOR_TENANT_ID,
        determination_set_id=result.determination_set_id,
    )
    from app.services.billing.starter_tax_evidence import billing_evidence

    prior = get_tax_determination_set_v2(
        db_session,
        tenant_id=OPERATOR_TENANT_ID,
        determination_set_id=vat.determination.determination_set_id,
    )
    assert (
        billing_evidence(owner_result, "expected_withholding", prior_results=(prior,))
        == withholding
    )
    binding["expected_withholding"]["prior_purposes"] = []
    assert starter_positions.starter_invoice_fact(db_session, invoice) == fact
    assert starter_positions.apply_fact_overlay(invoice, fact).balance_due == Decimal(
        "107.50"
    )


def test_explicit_zero_prior_is_retained_not_treated_as_missing(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.billing.starter_tax_evidence import (
        StarterInvoiceTaxPolicy,
        determine_invoice_line_tax,
    )

    binding = _publish_dependency_policy(db_session, monkeypatch, vat_rate=Decimal("0"))
    now = datetime.now(UTC)
    source_ref = f"zero:{uuid4()}"

    def determine(amount: str = "100.00") -> tuple[InvoiceTaxEvidenceV2, ...]:
        return determine_invoice_line_tax(
            db_session,
            policy=StarterInvoiceTaxPolicy.parse(binding),
            amount=Money.of(amount, Currency("NGN", 2)),
            source_ref=source_ref,
            source_version="1",
            counterparty_ref="fixture:party",
            supply_ref="fixture:supply",
            issued_at=now,
            occurred_on=now.date(),
        )

    evidence = determine()
    assert determine() == evidence
    with pytest.raises(DomainError) as reused_source:
        determine("101.00")
    assert reused_source.value.code.endswith("tax_refused")
    assert evidence[0].determination.tax_amount.amount == 0
    assert evidence[1].determination.components[0].base_amount.amount == Decimal(
        "100.00"
    )
    assert evidence[1].determination.tax_amount.amount == Decimal("5.00")
    assert evidence[1].prior_determinations == (evidence[0].determination,)


@pytest.mark.parametrize("omit_prior", (True, False))
def test_missing_prior_or_selected_component_refuses_without_issuance(
    db_session: Session,
    subscriber: Subscriber,
    monkeypatch: pytest.MonkeyPatch,
    omit_prior: bool,
) -> None:
    binding = _publish_dependency_policy(
        db_session,
        monkeypatch,
        selection="tax_code_ids",
        missing_selected_code=not omit_prior,
    )
    if omit_prior:
        binding["expected_withholding"]["prior_purposes"] = []
    with pytest.raises(DomainError) as refusal:
        _issue(db_session, subscriber)
    assert refusal.value.code.endswith("tax_refused")
    assert db_session.scalar(select(StarterInvoiceLink)) is None
    db_session.rollback()


def test_post_issuance_owner_failure_rolls_back_tax_fact_and_link_atomically(
    db_session: Session,
    subscriber: Subscriber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dotmac_tax import TaxNotFound

    from app.models.billing import Invoice

    _publish_dependency_policy(db_session, monkeypatch)
    subscriber.billing_mode = BillingMode.postpaid
    subscriber.billing_address_line1 = "2 Customer Close"
    subscriber.billing_city = "Abuja"
    invoice = _draft(db_session, subscriber)
    invoice_id = invoice.id

    def reject_audit(db: Session, event: AuditEventCreate) -> None:
        raise RuntimeError("injected owner failure after Billing issuance")

    monkeypatch.setattr(starter_issuance.AuditEvents, "stage", reject_audit)
    with pytest.raises(RuntimeError, match="injected owner failure"):
        starter_issuance.issue_admin_invoice_via_starter(
            db_session,
            invoice=invoice,
            actor_ref="user:rollback-proof",
        )
    fact = starter_positions.starter_invoice_fact(db_session, invoice)
    assert fact is not None
    tax_ids = tuple(
        item.determination.determination_set_id for item in fact.tax_evidence
    )
    # This service is a flush-only participant; the caller owns atomic rollback.
    db_session.rollback()
    assert (
        db_session.scalar(
            select(StarterInvoiceLink).where(
                StarterInvoiceLink.invoice_id == invoice_id
            )
        )
        is None
    )
    original = db_session.get(Invoice, invoice_id)
    assert original is not None and original.status is InvoiceStatus.draft
    for identity in tax_ids:
        with pytest.raises(TaxNotFound):
            get_tax_determination_set_v2(
                db_session, tenant_id=OPERATOR_TENANT_ID, determination_set_id=identity
            )


def test_multi_line_document_retains_each_original_commercial_identity(
    db_session: Session,
    subscriber: Subscriber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish_dependency_policy(db_session, monkeypatch)
    subscriber.billing_mode = BillingMode.postpaid
    subscriber.billing_address_line1 = "2 Customer Close"
    subscriber.billing_city = "Abuja"
    invoice = _draft(db_session, subscriber)
    first = invoice.lines[0]
    invoice.lines.append(
        InvoiceLine(
            description="Second original commercial line",
            quantity=Decimal("1"),
            unit_price=Decimal("200.00"),
            amount=Decimal("200.00"),
            tax_rate_id=first.tax_rate_id,
            tax_rate_code_snapshot=first.tax_rate_code_snapshot,
            tax_rate_percent_snapshot=first.tax_rate_percent_snapshot,
            tax_rate_snapshot_version=first.tax_rate_snapshot_version,
            tax_rate_is_active_snapshot=True,
            is_active=True,
        )
    )
    invoice.subtotal = Decimal("300.00")
    invoice.tax_total = Decimal("22.50")
    invoice.total = invoice.balance_due = Decimal("322.50")
    db_session.flush()
    starter_issuance.issue_admin_invoice_via_starter(
        db_session,
        invoice=invoice,
        actor_ref="user:multiline-dependency-proof",
    )
    fact = starter_positions.starter_invoice_fact(db_session, invoice)
    assert fact is not None
    assert len(fact.commercial_sources) == 2
    assert len(fact.tax_evidence) == 4
    assert {item.determination.source_amount.amount for item in fact.tax_evidence} == {
        Decimal("100.00"),
        Decimal("200.00"),
    }
    for source in fact.commercial_sources:
        principals = tuple(
            item
            for item in fact.tax_evidence
            if item.determination.commercial_source == source
        )
        assert len(principals) == 2
        assert {item.purpose for item in principals} == {
            "invoice_tax",
            "expected_withholding",
        }
        dependent = next(
            item for item in principals if item.purpose == "expected_withholding"
        )
        prior = dependent.prior_determinations[0]
        assert prior.commercial_source == source
        assert prior.source_amount == dependent.determination.source_amount
    assert fact.expected_withholding.amount == Decimal("16.13")
