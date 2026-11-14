"""Admin invoice issuance through Starter dotmac-billing (ADR 0031).

The web invoice-detail Issue action switches eligible drafts to the pinned
``dotmac-billing`` service as sole ledger writer: single-line quantity-one
drafts take the rated-invoice composition, homogeneous multi-line drafts
accept-per-line plus the multi-line draft-document composition. The Sub row
mirrors number, status and issued_at plus structural links. Ineligible drafts
(and every draft while the ``starter_ledger_live_enabled`` switch is OFF) keep
the legacy path; eligibility is decided before any write, and a Starter
failure after eligibility never falls back. Contract values resolve through
``starter_policies``; no Starter-contract
literal is inlined here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID

from dotmac_billing import (
    AcceptRatedObligationV1,
    AppliedFxSnapshotV1,
    AppliedTaxSnapshotV1,
    DueDateBasisV1,
    GetInvoiceDocumentFactV1,
    IssueRatedInvoiceV1,
    PartyDocumentSnapshotV1,
    PaymentInstructionsSnapshotV1,
    PostalAddressSnapshotV1,
    PresentationAssetReferenceV1,
    ServicePeriodEvidenceV1,
    get_invoice_document_fact,
    issue_rated_invoice,
)
from dotmac_billing.commands import (
    CreateMultiLineDraftDocument,
    DraftDocumentLine,
    IssueDocument,
)
from dotmac_billing.contracts import DueDateBasisStatus, ServicePeriodStatus
from dotmac_billing.errors import BillingError
from dotmac_billing.service import (
    accept_rated_obligation,
    create_billing_account,
    create_multi_line_draft_document,
    issue_document,
)
from dotmac_kernel.cache import Scope, TenantScope
from dotmac_kernel.money import Currency, Money, MoneyError
from dotmac_numbering import NumberingError, SeriesConfiguration, configure_series
from dotmac_numbering import allocate as numbering_allocate
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from dotmac_billing.models import BillingDocument, PlatformBillingDocument

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    StarterInvoiceLineLink,
    StarterInvoiceLink,
    TaxApplication,
)
from app.models.catalog import Subscription
from app.models.subscriber import Subscriber
from app.schemas.audit import AuditEventCreate
from app.services.audit import AuditEvents
from app.services.billing.starter_policies import (
    StarterBillingPolicies,
    StarterBillingPolicyError,
    collection_timing_for_billing_mode,
    due_terms_code,
    percent_to_fraction,
    resolve_starter_billing_policies,
    starter_ledger_live_enabled,
)
from app.services.brand_profiles import resolve_brand
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.invoice_bank_details import get_invoice_bank_details
from app.services.operator_tenant import OPERATOR_TENANT_ID

__all__ = [
    "AdministrativeEligibilityPolicy",
    "StarterIssuanceError",
    "eligibility_reason",
    "issue_admin_invoice_via_starter",
    "issue_prepaid_invoice_via_starter",
    "issue_recurring_invoice_via_starter",
    "prepaid_eligibility_reason",
    "recurring_eligibility_reason",
]

_SCOPE = TenantScope(OPERATOR_TENANT_ID)


class AdministrativeEligibilityPolicy(Enum):
    LEGACY_ARREARS = "legacy_arrears"
    FUTURE_CATALOGUE = "future_catalogue"


class StarterIssuanceError(DomainError, ValueError):
    """The Starter issuance lane refused or failed for this draft."""


def _error(suffix: str, message: str, **details: object) -> StarterIssuanceError:
    return StarterIssuanceError(
        code=f"financial.starter_issuance.{suffix}",
        message=message,
        details=details,
    )


class _ObligationSources:
    """Assembly obligation-source registry for one issuance lane."""

    def __init__(self, policies: StarterBillingPolicies, source_kind: str) -> None:
        self._policies = policies
        self._source_kind = source_kind

    def require_obligation_source(self, code: str) -> str:
        if code != self._source_kind:
            raise _error(
                "source_kind_undeclared",
                "Obligation source kind is not declared for Starter issuance",
                source_kind=code,
            )
        return self._policies.source_system


class _Numbering:
    """NumberingProvider over dotmac-numbering with policy provisioning."""

    def __init__(self, policies: StarterBillingPolicies) -> None:
        self._policies = policies

    def allocate(
        self,
        db: Session,
        *,
        scope: Scope,
        series_code: str,
        reference_date: date,
        idempotency_key: str,
    ) -> str:
        try:
            return numbering_allocate(
                db,
                scope=scope,
                series_code=series_code,
                reference_date=reference_date,
                idempotency_key=idempotency_key,
            ).formatted_number
        except NumberingError:
            configure_series(
                db,
                scope=scope,
                configuration=SeriesConfiguration(
                    series_code=series_code,
                    prefix=self._policies.series_prefix(series_code),
                    start_value=self._policies.series_start_value,
                ),
            )
            return numbering_allocate(
                db,
                scope=scope,
                series_code=series_code,
                reference_date=reference_date,
                idempotency_key=idempotency_key,
            ).formatted_number


def _money(amount: Decimal, currency: Currency) -> Money:
    try:
        return Money.of(str(amount), currency)
    except (MoneyError, ArithmeticError, ValueError) as exc:
        raise _error(
            "amount_unrepresentable",
            "Draft amount cannot be represented at the currency scale",
            amount=str(amount),
            currency=currency.code,
        ) from exc


def _active_lines(invoice: Invoice) -> list[InvoiceLine]:
    return [line for line in invoice.lines if line.is_active]


def _quantize(amount: Decimal, minor_units: int) -> Decimal:
    return amount.quantize(Decimal(1).scaleb(-minor_units), rounding=ROUND_HALF_UP)


def _line_tax_amount(line: InvoiceLine, minor_units: int) -> Decimal | None:
    """Derive one line's tax from its frozen snapshot, else ``None``.

    A line with no snapshot percent and no snapshot code is untaxed; a
    half-present snapshot is incomplete evidence and keeps the draft on
    the legacy lane.
    """

    if line.tax_application is TaxApplication.exempt:
        return Decimal("0.00")
    percent = line.tax_rate_percent_snapshot
    code = _clean(line.tax_rate_code_snapshot)
    if percent is None:
        return Decimal("0.00") if not code else None
    if not code:
        return None
    fraction = percent / Decimal("100")
    if line.tax_application is TaxApplication.inclusive:
        return _quantize(
            line.amount - line.amount / (Decimal("1") + fraction), minor_units
        )
    return _quantize(line.amount * fraction, minor_units)


def _line_pre_tax_amount(line: InvoiceLine, minor_units: int) -> Decimal:
    tax = _line_tax_amount(line, minor_units)
    if tax is None:
        raise _error(
            "line_tax_incomplete", "Invoice line lacks complete frozen tax evidence"
        )
    return (
        line.amount - tax
        if line.tax_application is TaxApplication.inclusive
        else line.amount
    )


def _line_net_unit_amount(line: InvoiceLine, minor_units: int) -> Decimal:
    net = _line_pre_tax_amount(line, minor_units)
    unit = _quantize(net / line.quantity, minor_units)
    if _quantize(unit * line.quantity, minor_units) != net:
        raise _error(
            "line_unit_unrepresentable",
            "Net unit price cannot represent this quantity at the currency scale",
        )
    return unit


def _future_catalogue_line(line: InvoiceLine) -> bool:
    return (
        isinstance(line.metadata_, dict)
        and line.metadata_.get("future_catalogue_invoice") is True
    )


def _clean(value: object) -> str:
    return str(value or "").strip()


def _country_code(policies: StarterBillingPolicies, *candidates: object) -> str:
    for candidate in candidates:
        code = _clean(candidate).upper()
        if len(code) == 3 and code.isalpha():
            return code
    return policies.default_country_code


def _seller_snapshot(
    db: Session, policies: StarterBillingPolicies, subscriber_id: object
) -> PartyDocumentSnapshotV1 | None:
    brand = resolve_brand(db, subscriber_id=str(subscriber_id))
    address = brand.legal_address or {}
    line_one = _clean(address.get("street1"))
    city = _clean(address.get("city"))
    if not _clean(brand.legal_name) or not line_one or not city:
        return None
    return PartyDocumentSnapshotV1(
        legal_name=_clean(brand.legal_name),
        address=PostalAddressSnapshotV1(
            line_one=line_one,
            line_two=_clean(address.get("street2")) or None,
            city=city,
            region=_clean(address.get("region")) or None,
            postal_code=_clean(address.get("postal_code")) or None,
            country_code=_country_code(policies, address.get("country")),
        ),
    )


def _customer_snapshot(
    policies: StarterBillingPolicies, subscriber: Subscriber
) -> PartyDocumentSnapshotV1 | None:
    legal_name = (
        _clean(subscriber.billing_name)
        or _clean(subscriber.company_name)
        or f"{_clean(subscriber.first_name)} {_clean(subscriber.last_name)}".strip()
    )
    line_one = _clean(subscriber.billing_address_line1) or _clean(
        subscriber.address_line1
    )
    line_two = _clean(subscriber.billing_address_line2) or _clean(
        subscriber.address_line2
    )
    city = _clean(subscriber.billing_city) or _clean(subscriber.city)
    if not legal_name or not line_one or not city:
        return None
    return PartyDocumentSnapshotV1(
        legal_name=legal_name,
        address=PostalAddressSnapshotV1(
            line_one=line_one,
            line_two=line_two or None,
            city=city,
            region=_clean(subscriber.billing_region) or None,
            postal_code=_clean(subscriber.billing_postal_code) or None,
            country_code=_country_code(policies, subscriber.billing_country_code),
        ),
    )


def eligibility_reason(
    db: Session,
    invoice: Invoice,
    *,
    allow_numbered: bool = False,
    require_timing: str = "arrears",
    administrative_policy: AdministrativeEligibilityPolicy = AdministrativeEligibilityPolicy.LEGACY_ARREARS,
) -> str | None:
    """Return why this draft cannot take the Starter lane yet, else None.

    Decided before any write: an eligible draft that then fails inside
    Starter never falls back to the legacy writer. ``allow_numbered``
    admits system-numbered drafts whose legacy number the Starter
    document number replaces at issuance (the scheduled cycle stages
    numbered drafts); staff-chosen numbers never qualify.
    """

    if not starter_ledger_live_enabled(db):
        return "Starter ledger live switch is OFF; legacy lane"
    if invoice.is_proforma:
        return "proforma invoices stay on the legacy lane"
    if invoice.status is not InvoiceStatus.draft:
        return "only drafts can be issued"
    if _clean(invoice.invoice_number) and not allow_numbered:
        return "manually numbered drafts stay on the legacy lane"
    if invoice.due_at is None:
        return "a due date is required for Starter issuance"
    if invoice.discount_amount != 0:
        return "discounted invoices stay on the legacy lane"
    policies = resolve_starter_billing_policies(db)
    lines = _active_lines(invoice)
    if not lines:
        return "draft has no active lines for Starter issuance"
    for line in lines:
        if line.quantity <= 0 or line.amount <= 0:
            return "non-positive line quantity or amount stays on the legacy lane"
        if (
            _quantize(line.quantity * line.unit_price, policies.default_minor_units)
            != line.amount
        ):
            return "line amounts do not reconcile for Starter issuance"
    if invoice.total != invoice.subtotal + invoice.tax_total:
        return "invoice totals do not reconcile for Starter issuance"
    if invoice.tax_total < 0 or invoice.total <= 0:
        return "non-positive totals stay on the legacy lane"
    line_taxes = [
        _line_tax_amount(line, policies.default_minor_units) for line in lines
    ]
    if any(tax is None for tax in line_taxes):
        return "taxed lines need a frozen tax rate snapshot"
    if sum((tax for tax in line_taxes if tax is not None), Decimal("0.00")) != (
        invoice.tax_total
    ):
        return "line taxes do not reconcile for Starter issuance"
    if (
        sum(
            (
                _line_pre_tax_amount(line, policies.default_minor_units)
                for line in lines
            ),
            Decimal("0.00"),
        )
        != invoice.subtotal
    ):
        return "line net amounts do not reconcile for Starter issuance"
    try:
        for line in lines:
            _line_net_unit_amount(line, policies.default_minor_units)
    except StarterIssuanceError:
        return "line net unit prices cannot be represented for Starter issuance"
    if len({line.subscription_id for line in lines}) != 1 and not all(
        _future_catalogue_line(line) for line in lines
    ):
        return "lines spanning multiple services stay on the legacy lane"
    subscriber = db.get(Subscriber, invoice.account_id)
    if subscriber is None:
        return "invoice account has no subscriber"
    try:
        timing = collection_timing_for_billing_mode(subscriber.billing_mode)
    except StarterBillingPolicyError:
        return "billing mode has no Starter collection timing mapping"
    if (
        timing == "advance"
        and administrative_policy is AdministrativeEligibilityPolicy.FUTURE_CATALOGUE
    ):
        reason = _administrative_advance_evidence_reason(invoice, lines)
        if reason is not None:
            return reason
    elif timing != require_timing:
        return "prepaid issuance stays on the legacy lane in this slice"
    for line in lines:
        if not _future_catalogue_line(line) or line.subscription_id is None:
            continue
        subscription = db.get(Subscription, line.subscription_id)
        if subscription is None or subscription.subscriber_id != invoice.account_id:
            return "catalogue line service does not belong to the invoice account"
        if collection_timing_for_billing_mode(subscription.billing_mode) != timing:
            return "catalogue service collection timing differs from the account lane"
    if _seller_snapshot(db, policies, subscriber.id) is None:
        return "seller legal identity or address is incomplete"
    if _customer_snapshot(policies, subscriber) is None:
        return "customer legal name or billing address is incomplete"
    if get_invoice_bank_details(db, currency=invoice.currency) is None:
        return "no collection bank account is configured for this currency"
    return None


def _service_period(
    invoice: Invoice, line: InvoiceLine | None = None
) -> ServicePeriodEvidenceV1:
    metadata = line.metadata_ if line is not None else None
    if (
        isinstance(metadata, dict)
        and metadata.get("future_catalogue_invoice") is True
        and metadata.get("kind") == "catalogue_one_time"
    ):
        return ServicePeriodEvidenceV1(status=ServicePeriodStatus.NOT_APPLICABLE)
    if isinstance(metadata, dict) and (
        "billing_period_start" in metadata or "billing_period_end" in metadata
    ):
        try:
            start = datetime.fromisoformat(str(metadata["billing_period_start"]))
            end = datetime.fromisoformat(str(metadata["billing_period_end"]))
            if (
                start.tzinfo is None
                or start.utcoffset() is None
                or end.tzinfo is None
                or end.utcoffset() is None
                or end <= start
            ):
                raise ValueError("invalid period")
        except (KeyError, TypeError, ValueError) as exc:
            raise _error(
                "line_period_invalid", "Invoice line service period evidence is invalid"
            ) from exc
        return ServicePeriodEvidenceV1(
            status=ServicePeriodStatus.VERIFIED, starts_at=start, ends_at=end
        )
    if invoice.billing_period_start is not None and (
        invoice.billing_period_end is not None
    ):
        return ServicePeriodEvidenceV1(
            status=ServicePeriodStatus.VERIFIED,
            starts_at=invoice.billing_period_start,
            ends_at=invoice.billing_period_end,
        )
    return ServicePeriodEvidenceV1(status=ServicePeriodStatus.UNKNOWN_UNVERIFIED)


def _administrative_advance_evidence_reason(
    invoice: Invoice, lines: list[InvoiceLine]
) -> str | None:
    """Admit only periods resolved by the existing future catalogue owner."""
    refusal = (
        "prepaid administrative issuance requires supported future catalogue evidence"
    )
    for line in lines:
        if not _future_catalogue_line(line):
            return refusal
        metadata = line.metadata_
        future = metadata.get("future_invoice")
        if not isinstance(future, dict):
            return refusal
        kind = future.get("period_kind")
        if kind == "one_time":
            if (
                line.subscription_id is not None
                or metadata.get("kind") != "catalogue_one_time"
            ):
                return refusal
        elif kind == "monthly":
            if line.subscription_id is None or not all(
                key in metadata
                for key in ("billing_period_start", "billing_period_end")
            ):
                return refusal
            try:
                if (
                    _service_period(invoice, line).status
                    is not ServicePeriodStatus.VERIFIED
                ):
                    return refusal
            except StarterIssuanceError:
                return refusal
        elif kind == "capped_pending_renewal":
            if (
                len(lines) != 1
                or line.quantity != Decimal("1")
                or line.subscription_id is None
                or "billing_period_start" in metadata
                or "billing_period_end" in metadata
                or invoice.billing_period_start is not None
                or invoice.billing_period_end is not None
            ):
                return refusal
        else:
            return refusal
    return None


def _tax_snapshots_for_line(
    policies: StarterBillingPolicies,
    line: InvoiceLine,
    currency: Currency,
) -> tuple[AppliedTaxSnapshotV1, ...]:
    if (
        line.tax_rate_percent_snapshot is None
        or line.tax_application is TaxApplication.exempt
    ):
        return ()
    tax_amount = _line_tax_amount(line, policies.default_minor_units)
    assert tax_amount is not None  # eligibility proved a complete snapshot
    return (
        AppliedTaxSnapshotV1(
            treatment_code=_clean(line.tax_rate_code_snapshot),
            jurisdiction_code=policies.tax_jurisdiction,
            policy_id=policies.tax_policy_id,
            policy_version=(
                str(line.tax_rate_snapshot_version)
                if line.tax_rate_snapshot_version is not None
                else policies.tax_policy_version
            ),
            rate=percent_to_fraction(line.tax_rate_percent_snapshot),
            taxable_basis=_money(
                _line_pre_tax_amount(line, policies.default_minor_units), currency
            ),
            tax_amount=_money(tax_amount, currency),
        ),
    )


def _obligation_for_line(
    policies: StarterBillingPolicies,
    *,
    account_id: UUID,
    subscriber: Subscriber,
    invoice: Invoice,
    line: InvoiceLine,
    currency: Currency,
    issued_at: datetime,
    source_fact_id: str,
    source_kind: str,
    contract_line_ref: str,
    contract_version: str,
    price_version: str,
    fx_observation_id: str,
) -> AcceptRatedObligationV1:
    tax_amount = _line_tax_amount(line, policies.default_minor_units)
    assert tax_amount is not None  # eligibility proved reconciliation
    return AcceptRatedObligationV1(
        scope=_SCOPE,
        billing_account_id=account_id,
        contract_line_ref=contract_line_ref,
        contract_version=contract_version,
        charge_component=policies.default_charge_component,
        source_system=policies.source_system,
        source_kind=source_kind,
        source_fact_id=source_fact_id,
        source_fact_version=policies.source_fact_version,
        subject_ref=policies.subject_ref(
            party_id=subscriber.party_id, subscriber_id=subscriber.id
        ),
        service_ref=(
            None
            if _future_catalogue_line(line) and line.subscription_id is None
            else policies.service_ref(
                subscription_id=line.subscription_id, subscriber_id=subscriber.id
            )
        ),
        service_period=_service_period(invoice, line),
        collection_timing=collection_timing_for_billing_mode(subscriber.billing_mode),
        pre_tax_amount=_money(
            _line_pre_tax_amount(line, policies.default_minor_units), currency
        ),
        tax_amount=_money(tax_amount, currency),
        total_amount=_money(
            _line_pre_tax_amount(line, policies.default_minor_units) + tax_amount,
            currency,
        ),
        rated_at=issued_at,
        price_version_id=price_version,
        tax_snapshots=_tax_snapshots_for_line(policies, line, currency),
        fx_snapshot=AppliedFxSnapshotV1(
            observation_id=fx_observation_id,
            observation_version=policies.fx_observation_version,
            base_currency=currency.code,
            quote_currency=currency.code,
            rate=Decimal("1.000000"),
            rate_purpose=policies.fx_rate_purpose,
            observed_at=issued_at,
            effective_at=issued_at,
            rounding_policy=policies.fx_rounding_policy,
            provenance=policies.fx_provenance,
        ),
    )


def _due_basis(
    policies: StarterBillingPolicies,
    invoice: Invoice,
    issued_at: datetime,
    evidence_ref: str,
) -> DueDateBasisV1:
    assert invoice.due_at is not None  # eligibility proved presence
    due_days = max((invoice.due_at.date() - issued_at.date()).days, 0)
    return DueDateBasisV1(
        status=DueDateBasisStatus.VERIFIED,
        source_authority=policies.source_system,
        evidence_ref=evidence_ref,
        payment_terms_code=due_terms_code(due_days),
        payment_terms_version=policies.due_terms_version,
        issued_at=issued_at,
        effective_at=issued_at,
        timezone=policies.default_timezone,
        derivation_policy=policies.due_derivation_policy,
        derivation_version=policies.due_derivation_version,
    )


def _payment_instructions(
    policies: StarterBillingPolicies, bank: dict
) -> PaymentInstructionsSnapshotV1:
    return PaymentInstructionsSnapshotV1(
        method_code=policies.payment_method_code,
        bank_name=bank["bank_name"],
        account_name=bank["account_name"],
        account_reference=bank["account_number"],
        routing_code=bank.get("sort_code") or None,
    )


@dataclass(frozen=True, slots=True)
class _IssuanceFlavor:
    """Lane-specific Starter contract identity for one issuance path."""

    source_kind: str
    contract_version: str
    price_version: str
    audit_reason: str


def _create_period_document(
    db: Session, *, command: CreateMultiLineDraftDocument, multi_period: bool
) -> BillingDocument | PlatformBillingDocument:
    """Leave the released a8 import surface usable; new issuance fails closed.

    Source preparation does not pretend that an unreleased API is installed.
    No legacy writer is invoked when the explicit multi-period lane refuses.
    """
    if not multi_period:
        return create_multi_line_draft_document(db, scope=_SCOPE, command=command)
    try:
        from dotmac_billing.commands import CreateMultiPeriodDraftDocumentV1
        from dotmac_billing.service import create_multi_period_draft_document_v1
    except ImportError as exc:
        raise _error(
            "multi_period_billing_unavailable",
            "Multi-period issuance requires the qualified Billing multi-period package and migration",
        ) from exc
    return create_multi_period_draft_document_v1(
        db,
        scope=_SCOPE,
        command=CreateMultiPeriodDraftDocumentV1(
            **{field.name: getattr(command, field.name) for field in fields(command)}
        ),
    )


def _issue_via_starter(
    db: Session,
    *,
    invoice: Invoice,
    actor_ref: str,
    issued_at: datetime,
    flavor: _IssuanceFlavor,
    contract_line_ref: Callable[[InvoiceLine], str],
    line_source_fact_id: Callable[[InvoiceLine], str],
    invoice_source_fact_id: str,
    fx_observation_id: str,
    due_evidence_ref: str,
    announce: bool,
) -> StarterInvoiceLink:
    """Issue one eligible draft through Starter as sole ledger writer.

    The caller owns the transaction boundary: on any failure nothing here
    may be committed, and the caller must not fall back to the legacy
    writer for this draft. Eligibility is decided by the caller before
    any write.
    """

    policies = resolve_starter_billing_policies(db)
    lines = _active_lines(invoice)
    subscriber = db.get(Subscriber, invoice.account_id)
    assert subscriber is not None  # eligibility proved presence
    seller = _seller_snapshot(db, policies, subscriber.id)
    customer = _customer_snapshot(policies, subscriber)
    bank = get_invoice_bank_details(db, currency=invoice.currency)
    assert seller is not None and customer is not None and bank is not None
    assert invoice.due_at is not None  # eligibility proved presence
    currency = Currency(invoice.currency.upper(), policies.default_minor_units)
    try:
        account = create_billing_account(
            db,
            scope=_SCOPE,
            external_account_ref=policies.account_ref(subscriber.id),
            currency=currency.code,
            minor_units=currency.minor_units,
        )
        if len(lines) == 1 and lines[0].quantity == 1:
            line = lines[0]
            issued = issue_rated_invoice(
                db,
                scope=_SCOPE,
                command=IssueRatedInvoiceV1(
                    obligation=_obligation_for_line(
                        policies,
                        account_id=account.id,
                        subscriber=subscriber,
                        invoice=invoice,
                        line=line,
                        currency=currency,
                        issued_at=issued_at,
                        source_fact_id=invoice_source_fact_id,
                        source_kind=flavor.source_kind,
                        contract_line_ref=contract_line_ref(line),
                        contract_version=flavor.contract_version,
                        price_version=flavor.price_version,
                        fx_observation_id=fx_observation_id,
                    ),
                    description=line.description,
                    unit_code=policies.default_unit_code,
                    seller_snapshot=seller,
                    customer_snapshot=customer,
                    payment_instructions=_payment_instructions(policies, bank),
                    brand_asset=PresentationAssetReferenceV1.none(),
                    locale=policies.default_locale,
                    timezone=policies.default_timezone,
                    document_profile_code=policies.document_profile_code,
                    document_profile_version=policies.document_profile_version,
                    series_code=policies.invoice_series,
                    reference_date=issued_at.date(),
                    due_at=invoice.due_at,
                    due_date_basis=_due_basis(
                        policies, invoice, issued_at, due_evidence_ref
                    ),
                    actor_ref=actor_ref,
                    correlation_id=policies.correlation_id(invoice.id),
                ),
                obligation_sources=_ObligationSources(policies, flavor.source_kind),
                numbering=_Numbering(policies),
            )
            fact = get_invoice_document_fact(
                db,
                scope=_SCOPE,
                request=GetInvoiceDocumentFactV1(
                    scope=_SCOPE,
                    invoice_id=issued.invoice_id,
                    fact_version=issued.issued_fact_version,
                ),
            )
            document_number = fact.fact.document_number
            document_id = issued.invoice_id
            issued_fact_version: int | None = issued.issued_fact_version
            link_obligation_id: UUID | None = issued.obligation_id
            line_obligations: list[tuple[InvoiceLine, UUID]] = [
                (line, issued.obligation_id)
            ]
        else:
            accepted = [
                accept_rated_obligation(
                    db,
                    scope=_SCOPE,
                    command=_obligation_for_line(
                        policies,
                        account_id=account.id,
                        subscriber=subscriber,
                        invoice=invoice,
                        line=line,
                        currency=currency,
                        issued_at=issued_at,
                        source_fact_id=line_source_fact_id(line),
                        source_kind=flavor.source_kind,
                        contract_line_ref=contract_line_ref(line),
                        contract_version=flavor.contract_version,
                        price_version=flavor.price_version,
                        fx_observation_id=fx_observation_id,
                    ),
                    accepted_source_kinds=frozenset({flavor.source_kind}),
                )
                for line in lines
            ]
            distinct_periods = {
                (
                    _service_period(invoice, line).status,
                    _service_period(invoice, line).starts_at,
                    _service_period(invoice, line).ends_at,
                )
                for line in lines
            }
            multi_period = (
                len(distinct_periods) > 1
                or len({line.subscription_id for line in lines}) > 1
                or len(lines) == 1
            )
            draft = _create_period_document(
                db,
                multi_period=multi_period,
                command=CreateMultiLineDraftDocument(
                    billing_account_id=account.id,
                    idempotency_key=invoice_source_fact_id,
                    lines=tuple(
                        DraftDocumentLine(
                            obligation_id=row.id,
                            description=line.description,
                            quantity=line.quantity,
                            unit_code=policies.default_unit_code,
                            unit_amount=_money(
                                _line_net_unit_amount(
                                    line, policies.default_minor_units
                                ),
                                currency,
                            ),
                        )
                        for line, row in zip(lines, accepted, strict=True)
                    ),
                    seller_snapshot=seller,
                    customer_snapshot=customer,
                    payment_instructions=_payment_instructions(policies, bank),
                    brand_asset=PresentationAssetReferenceV1.none(),
                    locale=policies.default_locale,
                    timezone=policies.default_timezone,
                    document_profile_code=policies.document_profile_code,
                    document_profile_version=policies.document_profile_version,
                    due_date_basis=_due_basis(
                        policies, invoice, issued_at, due_evidence_ref
                    ),
                ),
            )
            issued_document = issue_document(
                db,
                scope=_SCOPE,
                command=IssueDocument(
                    document_id=draft.id,
                    series_code=policies.invoice_series,
                    reference_date=issued_at.date(),
                    due_at=invoice.due_at,
                    due_date_basis=_due_basis(
                        policies, invoice, issued_at, due_evidence_ref
                    ),
                    actor_ref=actor_ref,
                    correlation_id=policies.correlation_id(invoice.id),
                ),
                numbering=_Numbering(policies),
            )
            number = issued_document.document_number
            if not number:
                raise _error(
                    "starter_refused",
                    "Starter billing issued no document number for this draft",
                    invoice_id=str(invoice.id),
                )
            document_number = number
            document_id = issued_document.id
            issued_fact_version = None
            link_obligation_id = None
            line_obligations = [
                (line, row.id) for line, row in zip(lines, accepted, strict=True)
            ]
    except StarterIssuanceError:
        raise
    except BillingError as exc:
        raise _error(
            "starter_refused",
            "Starter billing refused this issuance; the draft was not issued",
            invoice_id=str(invoice.id),
        ) from exc

    invoice.invoice_number = document_number
    invoice.status = InvoiceStatus.issued
    invoice.issued_at = issued_at
    link = StarterInvoiceLink(
        tenant_id=OPERATOR_TENANT_ID,
        invoice_id=invoice.id,
        billing_account_id=account.id,
        obligation_id=link_obligation_id,
        document_id=document_id,
        source_fact_id=invoice_source_fact_id,
        issued_fact_version=issued_fact_version,
    )
    db.add(link)
    if link_obligation_id is None:
        for line, obligation_id in line_obligations:
            db.add(
                StarterInvoiceLineLink(
                    tenant_id=OPERATOR_TENANT_ID,
                    invoice_line_id=line.id,
                    obligation_id=obligation_id,
                )
            )
    AuditEvents.stage(
        db,
        AuditEventCreate(
            action="issue_invoice_system",
            entity_type="invoice",
            entity_id=str(invoice.id),
            metadata_={
                "from_status": InvoiceStatus.draft.value,
                "to_status": InvoiceStatus.issued.value,
                "reason": flavor.audit_reason,
                "issued_at": issued_at.isoformat(),
                "issuance_lane": "starter",
                "actor_ref": actor_ref,
                "starter_document_id": str(document_id),
                "starter_obligation_ids": [
                    str(obligation_id) for _, obligation_id in line_obligations
                ],
                "source_fact_id": link.source_fact_id,
            },
        ),
    )
    if announce:
        emit_event(
            db,
            EventType.invoice_sent,
            {
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "amount": str(invoice.total),
                "total": str(invoice.total),
                "due_date": (
                    invoice.due_at.date().isoformat() if invoice.due_at else None
                ),
                "currency": invoice.currency,
                "from_status": InvoiceStatus.draft.value,
                "to_status": InvoiceStatus.issued.value,
            },
            account_id=invoice.account_id,
            invoice_id=invoice.id,
        )
    db.flush()
    return link


def _lock_and_refresh_issue_evidence(db: Session, invoice: Invoice) -> None:
    """Use the shared account fence before deciding the financial lane.

    The reservation owner refreshes membership, totals and scalar state after
    waiting for the lock. No eligibility computed before that refresh can
    authorize issuance.
    """
    from app.services.billing.invoices import Invoices

    Invoices.assert_service_period_reservations_for_issue(db, invoice)


def issue_admin_invoice_via_starter(
    db: Session,
    *,
    invoice: Invoice,
    actor_ref: str,
    announce: bool = False,
    administrative_policy: AdministrativeEligibilityPolicy = AdministrativeEligibilityPolicy.LEGACY_ARREARS,
) -> StarterInvoiceLink:
    """Issue one eligible admin draft through Starter as sole ledger writer."""

    _lock_and_refresh_issue_evidence(db, invoice)
    reason = eligibility_reason(
        db, invoice, administrative_policy=administrative_policy
    )
    if reason is not None:
        raise _error("ineligible", reason, invoice_id=str(invoice.id))
    policies = resolve_starter_billing_policies(db)
    return _issue_via_starter(
        db,
        invoice=invoice,
        actor_ref=actor_ref,
        issued_at=datetime.now(UTC),
        flavor=_IssuanceFlavor(
            source_kind=policies.admin_source_kind,
            contract_version=policies.admin_contract_version,
            price_version=policies.admin_price_version,
            audit_reason="admin_invoice_detail_issue",
        ),
        contract_line_ref=lambda line: policies.admin_contract_line_ref(line.id),
        line_source_fact_id=lambda line: policies.admin_line_source_fact_id(line.id),
        invoice_source_fact_id=policies.admin_source_fact_id(invoice.id),
        fx_observation_id=policies.admin_fx_observation_id(invoice.id),
        due_evidence_ref=policies.due_evidence_ref(invoice.id),
        announce=announce,
    )


def recurring_eligibility_reason(db: Session, invoice: Invoice) -> str | None:
    """Return why this cycle draft cannot take the Starter lane yet.

    Beyond the shared draft checks, the scheduled cycle lane requires a
    postpaid subscription behind every line and no spendable account
    credit: legacy credit application is not wired to Starter
    receivables in this slice, so credited accounts keep the legacy
    issuance path.
    """

    reason = eligibility_reason(db, invoice, allow_numbered=True)
    if reason is not None:
        return reason
    from app.models.catalog import Subscription
    from app.services.billing.account_credit import AccountCreditApplications

    subscription_ids = {line.subscription_id for line in _active_lines(invoice)}
    (subscription_id,) = subscription_ids  # shared checks proved exactly one
    if subscription_id is None:
        return "recurring lines need a subscription for Starter issuance"
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        return "recurring line subscription is missing"
    try:
        timing = collection_timing_for_billing_mode(subscription.billing_mode)
    except StarterBillingPolicyError:
        return "billing mode has no Starter collection timing mapping"
    if timing != "arrears":
        return "prepaid issuance stays on the legacy lane in this slice"
    preview = AccountCreditApplications.preview_invoice_funding(db, invoice)
    if preview.account_credit > 0:
        return "available account credit stays on the legacy lane in this slice"
    return None


def issue_recurring_invoice_via_starter(
    db: Session, *, invoice: Invoice, issued_at: datetime
) -> StarterInvoiceLink:
    """Issue one eligible scheduled-cycle draft through Starter."""

    _lock_and_refresh_issue_evidence(db, invoice)
    reason = recurring_eligibility_reason(db, invoice)
    if reason is not None:
        raise _error("ineligible", reason, invoice_id=str(invoice.id))
    policies = resolve_starter_billing_policies(db)
    due_ref = _clean(invoice.due_date_basis_ref) or policies.due_evidence_ref(
        invoice.id
    )
    return _issue_via_starter(
        db,
        invoice=invoice,
        actor_ref=policies.system_actor_ref,
        issued_at=issued_at,
        flavor=_IssuanceFlavor(
            source_kind=policies.recurring_source_kind,
            contract_version=policies.recurring_contract_version,
            price_version=policies.recurring_price_version,
            audit_reason="scheduled_billing_run",
        ),
        contract_line_ref=lambda line: policies.recurring_contract_line_ref(line.id),
        line_source_fact_id=lambda line: policies.recurring_line_source_fact_id(
            line.id
        ),
        invoice_source_fact_id=policies.recurring_source_fact_id(invoice.id),
        fx_observation_id=policies.recurring_fx_observation_id(invoice.id),
        due_evidence_ref=due_ref,
        announce=False,
    )


def prepaid_eligibility_reason(
    db: Session, invoice: Invoice, *, billing_line_key: str
) -> str | None:
    """Return why this prepaid period draft cannot take the Starter lane.

    Beyond the shared draft checks with advance collection timing,
    the prepaid lane requires exactly one active line carrying the
    funded period's ``billing_line_key`` so the issued obligation's
    source fact equals the earmark target funded for that period.
    """

    # Prepaid period drafts are always system-staged, and the invoice
    # owner auto-numbers at staging; the Starter document number
    # replaces that system number at issuance (the allow_numbered case).
    reason = eligibility_reason(
        db, invoice, require_timing="advance", allow_numbered=True
    )
    if reason is not None:
        return reason
    lines = _active_lines(invoice)
    if len(lines) != 1:
        return "prepaid period drafts need exactly one active line in this slice"
    if lines[0].billing_line_key != billing_line_key:
        return "prepaid period line does not carry the funded billing line key"
    return None


def issue_prepaid_invoice_via_starter(
    db: Session, *, invoice: Invoice, issued_at: datetime, billing_line_key: str
) -> StarterInvoiceLink:
    """Issue one earmark-funded prepaid period draft through Starter."""

    _lock_and_refresh_issue_evidence(db, invoice)
    reason = prepaid_eligibility_reason(db, invoice, billing_line_key=billing_line_key)
    if reason is not None:
        raise _error("ineligible", reason, invoice_id=str(invoice.id))
    policies = resolve_starter_billing_policies(db)
    due_ref = _clean(invoice.due_date_basis_ref) or policies.due_evidence_ref(
        invoice.id
    )
    return _issue_via_starter(
        db,
        invoice=invoice,
        actor_ref=policies.system_actor_ref,
        issued_at=issued_at,
        flavor=_IssuanceFlavor(
            source_kind=policies.prepaid_source_kind,
            contract_version=policies.prepaid_contract_version,
            price_version=policies.prepaid_price_version,
            audit_reason="prepaid_period_renewal",
        ),
        contract_line_ref=lambda line: policies.prepaid_contract_line_ref(
            billing_line_key
        ),
        line_source_fact_id=lambda line: policies.prepaid_line_source_fact_id(
            billing_line_key
        ),
        invoice_source_fact_id=policies.prepaid_source_fact_id(billing_line_key),
        fx_observation_id=policies.prepaid_fx_observation_id(invoice.id),
        due_evidence_ref=due_ref,
        announce=False,
    )
