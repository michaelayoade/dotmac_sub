"""Composable policies for consuming Starter money-pack contracts (ADR 0031).

Every assembly-supplied value the pinned ``dotmac-billing`` /
``dotmac-payments`` contracts require resolves here from domain settings with
typed defaults declared in ``settings_spec``; callers never inline
Starter-contract literals.

Account granularity is per Subscriber per currency (ADR 0031). No money writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.domain_errors import DomainError
from app.services.settings_spec import resolve_value

__all__ = [
    "StarterBillingPolicies",
    "StarterBillingPolicyError",
    "StarterSettlementPolicies",
    "account_ref_policy",
    "accepted_confirmation_evidence",
    "collection_timing_for_billing_mode",
    "due_terms_code",
    "percent_to_fraction",
    "StarterManualSettlementPolicies",
    "StarterPrepaidFundingPolicies",
    "StarterResellerSettlementPolicies",
    "resolve_starter_billing_policies",
    "resolve_starter_manual_settlement_policies",
    "resolve_starter_prepaid_funding_policies",
    "resolve_starter_reseller_settlement_policies",
    "resolve_starter_settlement_policies",
    "has_starter_invoice_link",
    "starter_ledger_live_enabled",
]


def starter_ledger_live_enabled(db: Session) -> bool:
    """D1 hold switch, fail closed: only a boolean ``True`` is ON."""
    value = resolve_value(db, SettingDomain.billing, "starter_ledger_live_enabled")
    return value is True


def has_starter_invoice_link(db: Session, invoice_id: UUID | None) -> bool:
    """Structural identity: the invoice was issued through Starter.

    Routes by the invoice's own link row (as settlement does), never by the
    switch, so an already Starter-issued invoice keeps its module intent.
    """
    if invoice_id is None:
        return False
    from sqlalchemy import select

    from app.models.billing import StarterInvoiceLink

    return (
        db.scalar(
            select(StarterInvoiceLink.id).where(
                StarterInvoiceLink.invoice_id == invoice_id
            )
        )
        is not None
    )


class StarterBillingPolicyError(DomainError, ValueError):
    """A Starter billing policy value is missing or malformed."""


def _error(suffix: str, message: str, **details: object) -> StarterBillingPolicyError:
    return StarterBillingPolicyError(
        code=f"financial.starter_billing_policies.{suffix}",
        message=message,
        details=details,
    )


def _text(db: Session, key: str) -> str:
    value = resolve_value(db, SettingDomain.billing, key)
    if not isinstance(value, str) or not value.strip():
        raise _error(
            "configuration_invalid",
            "Starter billing policy setting must be a non-blank string",
            setting=key,
        )
    return value.strip()


def _render_template(
    template: str, placeholder: str, value: UUID | str, *, setting: str
) -> str:
    rendered = template.replace("{" + placeholder + "}", str(value))
    if rendered == template or "{" in rendered:
        raise _error(
            "configuration_invalid",
            f"Template must contain {{{placeholder}}}",
            setting=setting,
        )
    return rendered


@dataclass(frozen=True, slots=True)
class StarterBillingPolicies:
    """Resolved Starter billing policy surface for one command assembly."""

    account_ref_template: str
    source_system: str
    admin_source_kind: str
    admin_contract_line_template: str
    admin_contract_version: str
    admin_price_version: str
    admin_fx_observation_template: str
    recurring_source_kind: str
    recurring_contract_line_template: str
    recurring_contract_version: str
    recurring_price_version: str
    recurring_source_fact_template: str
    recurring_line_source_fact_template: str
    recurring_fx_observation_template: str
    prepaid_source_kind: str
    prepaid_contract_line_template: str
    prepaid_contract_version: str
    prepaid_price_version: str
    prepaid_source_fact_template: str
    prepaid_line_source_fact_template: str
    prepaid_fx_observation_template: str
    system_actor_ref: str
    default_minor_units: int
    subject_party_template: str
    subject_subscriber_template: str
    service_subscription_template: str
    service_subscriber_template: str
    source_fact_version: str
    fx_observation_version: str
    fx_rate_purpose: str
    fx_rounding_policy: str
    fx_provenance: str
    payment_method_code: str
    correlation_template: str
    due_evidence_ref_template: str
    default_charge_component: str
    tax_jurisdiction: str
    tax_policy_id: str
    tax_policy_version: str
    due_terms_version: str
    due_derivation_policy: str
    due_derivation_version: str
    invoice_series: str
    credit_note_series: str
    document_profile_code: str
    document_profile_version: str
    credit_note_profile_code: str
    default_locale: str
    default_timezone: str
    default_unit_code: str
    admin_source_fact_template: str
    admin_line_source_fact_template: str
    invoice_series_prefix: str
    credit_note_series_prefix: str
    default_country_code: str
    series_start_value: int

    def account_ref(self, subscriber_id: UUID) -> str:
        return account_ref_policy(self.account_ref_template, subscriber_id)

    def series_prefix(self, series_code: str) -> str:
        if series_code == self.credit_note_series:
            return self.credit_note_series_prefix
        return self.invoice_series_prefix

    def admin_source_fact_id(self, invoice_id: UUID) -> str:
        return _render_template(
            self.admin_source_fact_template,
            "invoice_id",
            invoice_id,
            setting="starter_billing_admin_source_fact_template",
        )

    def admin_line_source_fact_id(self, line_id: UUID) -> str:
        return _render_template(
            self.admin_line_source_fact_template,
            "line_id",
            line_id,
            setting="starter_billing_admin_line_source_fact_template",
        )

    def admin_contract_line_ref(self, line_id: UUID) -> str:
        return _render_template(
            self.admin_contract_line_template,
            "line_id",
            line_id,
            setting="starter_billing_admin_contract_line_template",
        )

    def admin_fx_observation_id(self, invoice_id: UUID) -> str:
        return _render_template(
            self.admin_fx_observation_template,
            "invoice_id",
            invoice_id,
            setting="starter_billing_admin_fx_observation_template",
        )

    def recurring_source_fact_id(self, invoice_id: UUID) -> str:
        return _render_template(
            self.recurring_source_fact_template,
            "invoice_id",
            invoice_id,
            setting="starter_billing_recurring_source_fact_template",
        )

    def recurring_line_source_fact_id(self, line_id: UUID) -> str:
        return _render_template(
            self.recurring_line_source_fact_template,
            "line_id",
            line_id,
            setting="starter_billing_recurring_line_source_fact_template",
        )

    def recurring_contract_line_ref(self, line_id: UUID) -> str:
        return _render_template(
            self.recurring_contract_line_template,
            "line_id",
            line_id,
            setting="starter_billing_recurring_contract_line_template",
        )

    def recurring_fx_observation_id(self, invoice_id: UUID) -> str:
        return _render_template(
            self.recurring_fx_observation_template,
            "invoice_id",
            invoice_id,
            setting="starter_billing_recurring_fx_observation_template",
        )

    def prepaid_source_fact_id(self, billing_line_key: str) -> str:
        return _render_template(
            self.prepaid_source_fact_template,
            "billing_line_key",
            billing_line_key,
            setting="starter_billing_prepaid_source_fact_template",
        )

    def prepaid_line_source_fact_id(self, billing_line_key: str) -> str:
        return _render_template(
            self.prepaid_line_source_fact_template,
            "billing_line_key",
            billing_line_key,
            setting="starter_billing_prepaid_line_source_fact_template",
        )

    def prepaid_contract_line_ref(self, billing_line_key: str) -> str:
        return _render_template(
            self.prepaid_contract_line_template,
            "billing_line_key",
            billing_line_key,
            setting="starter_billing_prepaid_contract_line_template",
        )

    def prepaid_fx_observation_id(self, invoice_id: UUID) -> str:
        return _render_template(
            self.prepaid_fx_observation_template,
            "invoice_id",
            invoice_id,
            setting="starter_billing_prepaid_fx_observation_template",
        )

    def subject_ref(self, *, party_id: UUID | None, subscriber_id: UUID) -> str:
        if party_id is not None:
            return _render_template(
                self.subject_party_template,
                "party_id",
                party_id,
                setting="starter_billing_subject_party_template",
            )
        return _render_template(
            self.subject_subscriber_template,
            "subscriber_id",
            subscriber_id,
            setting="starter_billing_subject_subscriber_template",
        )

    def service_ref(self, *, subscription_id: UUID | None, subscriber_id: UUID) -> str:
        if subscription_id is not None:
            return _render_template(
                self.service_subscription_template,
                "subscription_id",
                subscription_id,
                setting="starter_billing_service_subscription_template",
            )
        return _render_template(
            self.service_subscriber_template,
            "subscriber_id",
            subscriber_id,
            setting="starter_billing_service_subscriber_template",
        )

    def correlation_id(self, invoice_id: UUID) -> str:
        return _render_template(
            self.correlation_template,
            "invoice_id",
            invoice_id,
            setting="starter_billing_correlation_template",
        )

    def due_evidence_ref(self, invoice_id: UUID) -> str:
        return _render_template(
            self.due_evidence_ref_template,
            "invoice_id",
            invoice_id,
            setting="starter_billing_due_evidence_ref_template",
        )


def resolve_starter_billing_policies(db: Session) -> StarterBillingPolicies:
    """Resolve every Starter billing policy value from domain settings."""

    return StarterBillingPolicies(
        account_ref_template=_text(db, "starter_billing_account_ref_template"),
        source_system=_text(db, "starter_billing_source_system"),
        admin_source_kind=_text(db, "starter_billing_admin_source_kind"),
        admin_contract_line_template=_text(
            db, "starter_billing_admin_contract_line_template"
        ),
        admin_contract_version=_text(db, "starter_billing_admin_contract_version"),
        admin_price_version=_text(db, "starter_billing_admin_price_version"),
        admin_fx_observation_template=_text(
            db, "starter_billing_admin_fx_observation_template"
        ),
        recurring_source_kind=_text(db, "starter_billing_recurring_source_kind"),
        recurring_contract_line_template=_text(
            db, "starter_billing_recurring_contract_line_template"
        ),
        recurring_contract_version=_text(
            db, "starter_billing_recurring_contract_version"
        ),
        recurring_price_version=_text(db, "starter_billing_recurring_price_version"),
        recurring_source_fact_template=_text(
            db, "starter_billing_recurring_source_fact_template"
        ),
        recurring_line_source_fact_template=_text(
            db, "starter_billing_recurring_line_source_fact_template"
        ),
        recurring_fx_observation_template=_text(
            db, "starter_billing_recurring_fx_observation_template"
        ),
        prepaid_source_kind=_text(db, "starter_billing_prepaid_source_kind"),
        prepaid_contract_line_template=_text(
            db, "starter_billing_prepaid_contract_line_template"
        ),
        prepaid_contract_version=_text(db, "starter_billing_prepaid_contract_version"),
        prepaid_price_version=_text(db, "starter_billing_prepaid_price_version"),
        prepaid_source_fact_template=_text(
            db, "starter_billing_prepaid_source_fact_template"
        ),
        prepaid_line_source_fact_template=_text(
            db, "starter_billing_prepaid_line_source_fact_template"
        ),
        prepaid_fx_observation_template=_text(
            db, "starter_billing_prepaid_fx_observation_template"
        ),
        system_actor_ref=_text(db, "starter_billing_system_actor_ref"),
        default_minor_units=_minor_units(db, "starter_billing_default_minor_units"),
        subject_party_template=_text(db, "starter_billing_subject_party_template"),
        subject_subscriber_template=_text(
            db, "starter_billing_subject_subscriber_template"
        ),
        service_subscription_template=_text(
            db, "starter_billing_service_subscription_template"
        ),
        service_subscriber_template=_text(
            db, "starter_billing_service_subscriber_template"
        ),
        source_fact_version=_text(db, "starter_billing_source_fact_version"),
        fx_observation_version=_text(db, "starter_billing_fx_observation_version"),
        fx_rate_purpose=_text(db, "starter_billing_fx_rate_purpose"),
        fx_rounding_policy=_text(db, "starter_billing_fx_rounding_policy"),
        fx_provenance=_text(db, "starter_billing_fx_provenance"),
        payment_method_code=_text(db, "starter_billing_payment_method_code"),
        correlation_template=_text(db, "starter_billing_correlation_template"),
        due_evidence_ref_template=_text(
            db, "starter_billing_due_evidence_ref_template"
        ),
        default_charge_component=_text(db, "starter_billing_default_charge_component"),
        tax_jurisdiction=_text(db, "starter_billing_tax_jurisdiction"),
        tax_policy_id=_text(db, "starter_billing_tax_policy_id"),
        tax_policy_version=_text(db, "starter_billing_tax_policy_version"),
        due_terms_version=_text(db, "starter_billing_due_terms_version"),
        due_derivation_policy=_text(db, "starter_billing_due_derivation_policy"),
        due_derivation_version=_text(db, "starter_billing_due_derivation_version"),
        invoice_series=_text(db, "starter_billing_invoice_series"),
        credit_note_series=_text(db, "starter_billing_credit_note_series"),
        document_profile_code=_text(db, "starter_billing_document_profile_code"),
        document_profile_version=_text(db, "starter_billing_document_profile_version"),
        credit_note_profile_code=_text(db, "starter_billing_credit_note_profile_code"),
        default_locale=_text(db, "starter_billing_default_locale"),
        default_timezone=_text(db, "starter_billing_default_timezone"),
        default_unit_code=_text(db, "starter_billing_default_unit_code"),
        admin_source_fact_template=_text(
            db, "starter_billing_admin_source_fact_template"
        ),
        admin_line_source_fact_template=_text(
            db, "starter_billing_admin_line_source_fact_template"
        ),
        invoice_series_prefix=_text(db, "starter_billing_invoice_series_prefix"),
        credit_note_series_prefix=_text(
            db, "starter_billing_credit_note_series_prefix"
        ),
        default_country_code=_text(db, "starter_billing_default_country_code"),
        series_start_value=_positive_int(db, "starter_billing_series_start_value"),
    )


def _minor_units(db: Session, key: str) -> int:
    value = resolve_value(db, SettingDomain.billing, key)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _error(
            "configuration_invalid",
            "Starter billing minor units must be an integer from 0 to 6",
            setting=key,
        )
    try:
        parsed = int(value)
    except ValueError as exc:
        raise _error(
            "configuration_invalid",
            "Starter billing minor units must be an integer from 0 to 6",
            setting=key,
        ) from exc
    if not 0 <= parsed <= 6:
        raise _error(
            "configuration_invalid",
            "Starter billing minor units must be an integer from 0 to 6",
            setting=key,
        )
    return parsed


def _positive_int(db: Session, key: str) -> int:
    value = resolve_value(db, SettingDomain.billing, key)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _error(
            "configuration_invalid",
            "Starter billing policy setting must be a positive integer",
            setting=key,
        )
    try:
        parsed = int(value)
    except ValueError as exc:
        raise _error(
            "configuration_invalid",
            "Starter billing policy setting must be a positive integer",
            setting=key,
        ) from exc
    if parsed < 1:
        raise _error(
            "configuration_invalid",
            "Starter billing policy setting must be a positive integer",
            setting=key,
        )
    return parsed


def account_ref_policy(template: str, subscriber_id: UUID) -> str:
    """Render the Starter external account reference for one Subscriber."""

    rendered = template.replace("{subscriber_id}", str(subscriber_id))
    if rendered == template or "{" in rendered or not rendered.strip():
        raise _error(
            "configuration_invalid",
            "Account reference template must contain {subscriber_id}",
            setting="starter_billing_account_ref_template",
        )
    return rendered


def accepted_confirmation_evidence(db: Session) -> frozenset[str]:
    """Evidence tokens Sub accepts as independently confirmed settlement."""

    raw = _text(db, "starter_payments_accepted_evidence")
    tokens = frozenset(token.strip() for token in raw.split(",") if token.strip())
    if not tokens:
        raise _error(
            "configuration_invalid",
            "At least one accepted confirmation evidence token is required",
            setting="starter_payments_accepted_evidence",
        )
    return tokens


def collection_timing_for_billing_mode(
    billing_mode: object,
) -> Literal["advance", "arrears"]:
    """Map Sub billing mode to Starter collection timing.

    Prepaid service is paid in advance; postpaid service is billed in
    arrears. Unknown modes refuse rather than guess.
    """

    value = getattr(billing_mode, "value", billing_mode)
    normalized = str(value or "").strip().lower()
    if normalized == "prepaid":
        return "advance"
    if normalized == "postpaid":
        return "arrears"
    raise _error(
        "billing_mode_unmapped",
        "Billing mode has no Starter collection timing mapping",
        billing_mode=normalized or None,
    )


def due_terms_code(due_days: int) -> str:
    """Derive the payment-terms code for a day-count due policy."""

    if due_days < 0:
        raise _error(
            "due_days_invalid",
            "Payment due days cannot be negative",
            due_days=due_days,
        )
    return f"net_{due_days}"


def percent_to_fraction(percent: Decimal) -> Decimal:
    """Convert a Sub tax percent snapshot (20.00) to a Starter rate (0.2)."""

    try:
        return (percent / Decimal("100")).normalize()
    except (InvalidOperation, ArithmeticError) as exc:
        raise _error(
            "tax_rate_invalid",
            "Tax percent snapshot cannot be converted to a rate fraction",
        ) from exc


@dataclass(frozen=True, slots=True)
class StarterSettlementPolicies:
    """Resolved Starter settlement policy surface (ADR 0031).

    The presented confirmation-evidence token must be a member of the
    accepted-evidence set billing is asked to trust; anything else is a
    configuration refusal, never a silent substitution.
    """

    settlement_source_system: str
    settlement_source_version: str
    funding_lane: str
    settlement_source_ref_template: str
    confirmation_evidence: str
    accepted_confirmation_evidence: frozenset[str]

    def settlement_source_ref(self, confirmation_id: UUID) -> str:
        return _render_template(
            self.settlement_source_ref_template,
            "confirmation_id",
            confirmation_id,
            setting="starter_billing_settlement_source_ref_template",
        )


def resolve_starter_settlement_policies(db: Session) -> StarterSettlementPolicies:
    accepted = accepted_confirmation_evidence(db)
    presented = _text(db, "starter_payments_confirmation_evidence")
    if presented not in accepted:
        raise _error(
            "configuration_invalid",
            "Presented confirmation evidence is not in the accepted set",
            setting="starter_payments_confirmation_evidence",
        )
    return StarterSettlementPolicies(
        settlement_source_system=_text(db, "starter_payments_settlement_source_system"),
        settlement_source_version=_text(
            db, "starter_billing_settlement_source_version"
        ),
        funding_lane=_text(db, "starter_billing_settlement_funding_lane"),
        settlement_source_ref_template=_text(
            db, "starter_billing_settlement_source_ref_template"
        ),
        confirmation_evidence=presented,
        accepted_confirmation_evidence=accepted,
    )


@dataclass(frozen=True, slots=True)
class StarterCreditPolicies:
    """Resolved Starter credit-note policy surface (ADR 0031)."""

    credit_source_fact_template: str
    credit_correlation_template: str

    def credit_source_fact_id(self, credit_note_id: UUID) -> str:
        return _render_template(
            self.credit_source_fact_template,
            "credit_note_id",
            credit_note_id,
            setting="starter_billing_credit_source_fact_template",
        )

    def credit_correlation_id(self, idempotency_key: str) -> str:
        return _render_template(
            self.credit_correlation_template,
            "idempotency_key",
            idempotency_key,
            setting="starter_billing_credit_correlation_template",
        )


def resolve_starter_credit_policies(db: Session) -> StarterCreditPolicies:
    return StarterCreditPolicies(
        credit_source_fact_template=_text(
            db, "starter_billing_credit_source_fact_template"
        ),
        credit_correlation_template=_text(
            db, "starter_billing_credit_correlation_template"
        ),
    )


@dataclass(frozen=True, slots=True)
class StarterManualSettlementPolicies:
    """Resolved Starter manual-settlement policy surface (ADR 0031)."""

    manual_intent_reference_template: str
    manual_confirmation_source: str
    manual_intent_provider_type: str
    manual_intent_channel: str
    manual_intent_expiry_hours: int

    def manual_intent_reference(self, idempotency_key: str) -> str:
        return _render_template(
            self.manual_intent_reference_template,
            "idempotency_key",
            idempotency_key,
            setting="starter_payments_manual_intent_reference_template",
        )


def resolve_starter_manual_settlement_policies(
    db: Session,
) -> StarterManualSettlementPolicies:
    return StarterManualSettlementPolicies(
        manual_intent_reference_template=_text(
            db, "starter_payments_manual_intent_reference_template"
        ),
        manual_confirmation_source=_text(
            db, "starter_payments_manual_confirmation_source"
        ),
        manual_intent_provider_type=_text(
            db, "starter_payments_manual_intent_provider_type"
        ),
        manual_intent_channel=_text(db, "starter_payments_manual_intent_channel"),
        manual_intent_expiry_hours=_positive_int(
            db, "starter_payments_manual_intent_expiry_hours"
        ),
    )


@dataclass(frozen=True, slots=True)
class StarterResellerSettlementPolicies:
    """Resolved Starter reseller consolidated-settlement policies (ADR 0031)."""

    reseller_intent_reference_template: str
    reseller_target_ref_template: str
    reseller_confirmation_source: str
    reseller_intent_provider_type: str
    reseller_intent_channel: str
    reseller_intent_expiry_hours: int
    reseller_slice_source_key_template: str
    reseller_allocation_source_ref_template: str

    def reseller_intent_reference(self, payment_id: UUID) -> str:
        return _render_template(
            self.reseller_intent_reference_template,
            "payment_id",
            payment_id,
            setting="starter_payments_reseller_intent_reference_template",
        )

    def reseller_target_ref(self, billing_account_id: UUID) -> str:
        return _render_template(
            self.reseller_target_ref_template,
            "billing_account_id",
            billing_account_id,
            setting="starter_payments_reseller_target_ref_template",
        )

    def reseller_slice_source_key(
        self, *, confirmation_id: UUID, subscriber_id: UUID
    ) -> str:
        rendered = self.reseller_slice_source_key_template.replace(
            "{confirmation_id}", str(confirmation_id)
        ).replace("{subscriber_id}", str(subscriber_id))
        if rendered == self.reseller_slice_source_key_template or "{" in rendered:
            raise _error(
                "configuration_invalid",
                "Template must contain {confirmation_id} and {subscriber_id}",
                setting="starter_billing_reseller_slice_source_key_template",
            )
        return rendered

    def reseller_allocation_source_ref(self, slice_source_key: str) -> str:
        return _render_template(
            self.reseller_allocation_source_ref_template,
            "slice_source_key",
            slice_source_key,
            setting="starter_billing_reseller_allocation_source_ref_template",
        )


def resolve_starter_reseller_settlement_policies(
    db: Session,
) -> StarterResellerSettlementPolicies:
    return StarterResellerSettlementPolicies(
        reseller_intent_reference_template=_text(
            db, "starter_payments_reseller_intent_reference_template"
        ),
        reseller_target_ref_template=_text(
            db, "starter_payments_reseller_target_ref_template"
        ),
        reseller_confirmation_source=_text(
            db, "starter_payments_reseller_confirmation_source"
        ),
        reseller_intent_provider_type=_text(
            db, "starter_payments_reseller_intent_provider_type"
        ),
        reseller_intent_channel=_text(db, "starter_payments_reseller_intent_channel"),
        reseller_intent_expiry_hours=_positive_int(
            db, "starter_payments_reseller_intent_expiry_hours"
        ),
        reseller_slice_source_key_template=_text(
            db, "starter_billing_reseller_slice_source_key_template"
        ),
        reseller_allocation_source_ref_template=_text(
            db, "starter_billing_reseller_allocation_source_ref_template"
        ),
    )


@dataclass(frozen=True, slots=True)
class StarterPrepaidFundingPolicies:
    """Resolved Starter prepaid funding policy surface (ADR 0031)."""

    prepaid_confirmation_source: str
    prepaid_funding_lane: str
    prepaid_earmark_source_ref_template: str
    prepaid_allocation_source_ref_template: str

    def prepaid_earmark_source_ref(self, confirmation_id: UUID) -> str:
        return _render_template(
            self.prepaid_earmark_source_ref_template,
            "confirmation_id",
            confirmation_id,
            setting="starter_billing_prepaid_earmark_source_ref_template",
        )

    def prepaid_allocation_source_ref(self, source_fact_id: str) -> str:
        # One confirmation funds many earmarked periods, so the allocation
        # idempotency identity is the period target, never the confirmation.
        return _render_template(
            self.prepaid_allocation_source_ref_template,
            "source_fact_id",
            source_fact_id,
            setting="starter_billing_prepaid_allocation_source_ref_template",
        )


def resolve_starter_prepaid_funding_policies(
    db: Session,
) -> StarterPrepaidFundingPolicies:
    return StarterPrepaidFundingPolicies(
        prepaid_confirmation_source=_text(
            db, "starter_payments_prepaid_confirmation_source"
        ),
        prepaid_funding_lane=_text(db, "starter_billing_prepaid_funding_lane"),
        prepaid_earmark_source_ref_template=_text(
            db, "starter_billing_prepaid_earmark_source_ref_template"
        ),
        prepaid_allocation_source_ref_template=_text(
            db, "starter_billing_prepaid_allocation_source_ref_template"
        ),
    )
