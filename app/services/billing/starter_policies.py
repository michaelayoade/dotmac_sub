"""Composable policies for consuming Starter money-pack contracts (ADR 0031).

Every assembly-supplied value the pinned ``dotmac-billing`` /
``dotmac-payments`` contracts require — account identity, obligation
identity, tax and due-date policy evidence, numbering series, document
profiles, accepted confirmation evidence — resolves here from domain
settings with typed defaults declared in ``settings_spec``. Callers never
inline Starter-contract literals; changing a convention is a settings
change, not a release.

Account granularity is per Subscriber per currency (ADR 0031): a person
with linked radio and fiber accounts holds independent ledger accounts;
person identity travels as subject, service heterogeneity as service
reference and charge component. Resolution is deterministic and performs
no money-state writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.domain_errors import DomainError
from app.services.settings_spec import resolve_value

__all__ = [
    "StarterBillingPolicies",
    "StarterBillingPolicyError",
    "account_ref_policy",
    "accepted_confirmation_evidence",
    "collection_timing_for_billing_mode",
    "due_terms_code",
    "percent_to_fraction",
    "resolve_starter_billing_policies",
]


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


@dataclass(frozen=True, slots=True)
class StarterBillingPolicies:
    """Resolved Starter billing policy surface for one command assembly."""

    account_ref_template: str
    source_system: str
    admin_source_kind: str
    admin_contract_line_template: str
    admin_contract_version: str
    admin_price_version: str
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

    def account_ref(self, subscriber_id: UUID) -> str:
        return account_ref_policy(self.account_ref_template, subscriber_id)

    def admin_contract_line_ref(self, line_id: UUID) -> str:
        rendered = self.admin_contract_line_template.replace("{line_id}", str(line_id))
        if rendered == self.admin_contract_line_template or "{" in rendered:
            raise _error(
                "configuration_invalid",
                "Admin contract line template must contain {line_id}",
                setting="starter_billing_admin_contract_line_template",
            )
        return rendered


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
    )


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


def collection_timing_for_billing_mode(billing_mode: object) -> str:
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
