"""Staff-recorded manual payments against Starter-issued invoices.

Manual lane (ADR 0031): a staff-recorded payment whose confirmed
allocation lands in full on one Starter-linked invoice opens a
``dotmac-payments`` intent bound to that invoice, records the staff
evidence as a confirmation (policy-resolved source, default
``TRANSFER_PROOF``), and discharges through the shared Starter
settlement owner — billing acceptance, allocation, mirror, and the
exactly-once access signal are identical to the webhook lane. No
legacy ``Payment`` or legacy ledger entry is written; the manual
recording owner presents the Starter confirmation as its payment
projection. Replays of one manual recording derive the same intent
reference from its idempotency key, so the settlement replay returns
the same Starter identities.

The caller (manual payment recording) owns eligibility, duplicate
review, locking, and the transaction boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money, MoneyError
from dotmac_payments import (
    ConfirmationSource,
    OpenPaymentIntent,
    PaymentError,
    PaymentPurpose,
    open_payment_intent,
)
from sqlalchemy.orm import Session

from app.models.billing import Invoice, StarterInvoiceLink
from app.services.billing.starter_policies import (
    StarterManualSettlementPolicies,
    resolve_starter_billing_policies,
    resolve_starter_manual_settlement_policies,
)
from app.services.billing.starter_settlement import (
    StarterSettlementResult,
    settle_starter_invoice_payment,
)
from app.services.domain_errors import DomainError
from app.services.operator_tenant import OPERATOR_TENANT_ID

__all__ = [
    "StarterManualSettlement",
    "StarterManualSettlementError",
    "record_manual_starter_payment",
    "ManualStarterAccountDeposit",
    "StarterAccountDepositEvidence",
    "StarterManualAccountDeposit",
    "record_manual_starter_account_deposit",
]

_SCOPE = TenantScope(OPERATOR_TENANT_ID)
_OWNER = "financial.starter_manual_settlement"


class StarterManualSettlementError(DomainError, ValueError):
    """The Starter manual settlement lane refused or failed."""


def _error(
    suffix: str, message: str, **details: object
) -> StarterManualSettlementError:
    return StarterManualSettlementError(
        code=f"{_OWNER}.{suffix}", message=message, details=details
    )


@dataclass(frozen=True, slots=True)
class StarterManualSettlement:
    """Starter identities recorded for one manual invoice payment."""

    intent_reference: str
    settlement: StarterSettlementResult


def _confirmation_source(
    policies: StarterManualSettlementPolicies,
) -> ConfirmationSource:
    try:
        return ConfirmationSource(policies.manual_confirmation_source)
    except ValueError as exc:
        raise _error(
            "configuration_invalid",
            "Manual confirmation source is not a Starter confirmation source",
            confirmation_source=policies.manual_confirmation_source,
        ) from exc


def record_manual_starter_payment(
    db: Session,
    *,
    invoice: Invoice,
    link: StarterInvoiceLink,
    amount: Decimal,
    external_reference: str,
    occurred_at: datetime,
    idempotency_key: str,
) -> StarterManualSettlement:
    """Open a bound intent and discharge one Starter invoice manually."""
    policies = resolve_starter_manual_settlement_policies(db)
    billing_policies = resolve_starter_billing_policies(db)
    currency = Currency(invoice.currency.upper(), billing_policies.default_minor_units)
    try:
        requested = Money(amount, currency)
    except MoneyError as exc:
        raise _error(
            "amount_unrepresentable",
            "Manual amount cannot be represented in the invoice currency",
            invoice_id=str(invoice.id),
        ) from exc
    reference = policies.manual_intent_reference(idempotency_key)
    try:
        open_payment_intent(
            db,
            scope=_SCOPE,
            command=OpenPaymentIntent(
                reference=reference,
                payer_reference=str(invoice.account_id),
                purpose=PaymentPurpose.INVOICE_SETTLEMENT,
                target_reference=str(invoice.id),
                requested=requested,
                provider_type=policies.manual_intent_provider_type,
                channel=policies.manual_intent_channel,
                opened_at=occurred_at,
                expires_at=occurred_at
                + timedelta(hours=policies.manual_intent_expiry_hours),
            ),
        )
    except PaymentError as exc:
        raise _error(
            "starter_refused",
            "Starter payments refused the manual payment intent",
            invoice_id=str(invoice.id),
            reference=reference,
        ) from exc
    settlement = settle_starter_invoice_payment(
        db,
        invoice=invoice,
        link=link,
        intent_reference=reference,
        external_reference=external_reference,
        confirmed_amount=amount,
        occurred_at=occurred_at,
        observed_at=occurred_at,
        confirmation_source=_confirmation_source(policies),
    )
    return StarterManualSettlement(intent_reference=reference, settlement=settlement)


@dataclass(frozen=True, slots=True)
class ManualStarterAccountDeposit:
    account_id: UUID
    amount: Decimal
    currency: str
    external_reference: str
    idempotency_key: str
    paid_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StarterAccountDepositEvidence:
    confirmation_id: UUID
    settlement_id: UUID
    billing_account_id: UUID
    amount: Money
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class StarterManualAccountDeposit:
    intent_reference: str
    settlement: StarterAccountDepositEvidence


def native_account_deposit_enabled(db: Session, *, account_id: UUID) -> bool:
    """Qualify the bounded WHT cash lane without adopting historical money."""
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec
    from app.services.billing.starter_policies import starter_ledger_live_enabled
    from app.services.customer_tax_policies import get_customer_withholding_tax_policy
    from app.services.invoice_withholding import (
        NativeWithholdingTaxBindingQuery,
        query_native_withholding_tax_binding,
    )

    if (
        not starter_ledger_live_enabled(db)
        or not get_customer_withholding_tax_policy(
            db, account_id=account_id
        ).withholding_tax_enabled
    ):
        return False
    jurisdiction = settings_spec.resolve_value(
        db, SettingDomain.billing, "starter_billing_withholding_tax_jurisdiction_id"
    )
    tax_code = settings_spec.resolve_value(
        db, SettingDomain.billing, "starter_billing_withholding_tax_code"
    )
    if not jurisdiction and not tax_code:
        # No native Tax binding is an unactivated feature, not cash adoption.
        return False
    try:
        from dotmac_billing.contracts import (
            GetBillingAccountByExternalRefV1,
            GetCurrentInvoiceReceivableV1,
        )
        from dotmac_tax.service import preview_tax_policy

        _ = (
            GetBillingAccountByExternalRefV1,
            GetCurrentInvoiceReceivableV1,
            preview_tax_policy,
        )
    except ImportError as exc:
        raise _error(
            "configuration_invalid",
            "Qualified native Billing and Tax capabilities are required before recording this WHT account deposit.",
        ) from exc
    query_native_withholding_tax_binding(
        db, NativeWithholdingTaxBindingQuery(OPERATOR_TENANT_ID)
    )
    return True


def record_manual_starter_account_deposit(
    db: Session, *, command: ManualStarterAccountDeposit
) -> StarterManualAccountDeposit:
    """Record NEW actual staff-attested cash through existing native writers.

    Historical local receipts never enter this API as a second confirmation.
    The manual root owner supplies the actual bank reference and duplicate review.
    Payments owns first-observed time and exact replay; Billing claims that one
    confirmed-cash identity as unallocated credit. No legacy money row is staged.
    """
    from dotmac_billing import AcceptSettlementV1, SettlementFundingLane
    from dotmac_billing.service import accept_settlement, create_billing_account
    from dotmac_payments import (
        GetConfirmedCashV1,
        RecordConfirmation,
        get_confirmed_cash_v1,
        record_confirmation,
    )

    from app.services.billing.starter_policies import (
        resolve_starter_settlement_policies,
    )
    from app.services.owner_commands import owner_command_active

    if not owner_command_active(db, owner="financial.manual_payment_recording"):
        raise _error(
            "owner_required",
            "Native account cash must participate in the registered manual receipt owner.",
        )
    if not command.external_reference.strip() or not command.idempotency_key.strip():
        raise _error(
            "evidence_required",
            "Record the actual bank receipt reference and idempotency identity.",
        )
    policies = resolve_starter_manual_settlement_policies(db)
    billing_policies = resolve_starter_billing_policies(db)
    settlement_policies = resolve_starter_settlement_policies(db)
    if settlement_policies.funding_lane != SettlementFundingLane.AVAILABLE_CREDIT.value:
        raise _error(
            "configuration_invalid",
            "Account deposits require the native available-credit funding policy.",
        )
    money = Money(
        command.amount, Currency(command.currency, billing_policies.default_minor_units)
    )
    reference = policies.manual_intent_reference(
        f"account-deposit:{command.idempotency_key}"
    )
    try:
        account = create_billing_account(
            db,
            scope=_SCOPE,
            external_account_ref=billing_policies.account_ref(command.account_id),
            currency=money.currency.code,
            minor_units=money.currency.minor_units,
        )
        intent = open_payment_intent(
            db,
            scope=_SCOPE,
            command=OpenPaymentIntent(
                reference=reference,
                payer_reference=str(command.account_id),
                purpose=PaymentPurpose.ACCOUNT_CREDIT_DEPOSIT,
                target_reference=str(command.account_id),
                requested=money,
                provider_type=policies.manual_intent_provider_type,
                channel=policies.manual_intent_channel,
            ),
        )
        # Omitting observed_at deliberately preserves the first native observation
        # on replay, as RecordConfirmation's existing contract requires.
        confirmation = record_confirmation(
            db,
            scope=_SCOPE,
            command=RecordConfirmation(
                intent_id=intent.id,
                source=_confirmation_source(policies),
                external_reference=command.external_reference,
                confirmed=money,
            ),
        )
        cash = get_confirmed_cash_v1(
            db, scope=_SCOPE, query=GetConfirmedCashV1(confirmation.id)
        )
        if cash.intent_id != intent.id or cash.confirmed != money:
            raise _error(
                "cash_binding_mismatch",
                "Native cash observation does not match this deposit intent.",
            )
        settlement = accept_settlement(
            db,
            scope=_SCOPE,
            command=AcceptSettlementV1(
                scope=_SCOPE,
                billing_account_id=account.id,
                source_system=settlement_policies.settlement_source_system,
                source_settlement_key=str(cash.confirmation_id),
                source_version=settlement_policies.settlement_source_version,
                amount=cash.confirmed,
                occurred_at=command.paid_at or cash.observed_at,
                observed_at=cash.observed_at,
                confirmation_evidence=settlement_policies.confirmation_evidence,
                funding_lane=SettlementFundingLane.AVAILABLE_CREDIT,
            ),
            accepted_confirmation_evidence=settlement_policies.accepted_confirmation_evidence,
        )
    except PaymentError as exc:
        raise _error(
            "starter_refused",
            "Native Payments refused the receipt evidence; no local cash fallback is allowed.",
        ) from exc
    return StarterManualAccountDeposit(
        reference,
        StarterAccountDepositEvidence(
            cash.confirmation_id,
            settlement.id,
            account.id,
            cash.confirmed,
            cash.observed_at,
        ),
    )
