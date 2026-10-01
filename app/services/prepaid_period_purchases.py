"""Owner for quoted multi-period prepaid purchases and exact settlement."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.billing import (
    InvoiceDueDateBasis,
    InvoiceStatus,
    Payment,
    PaymentProvider,
    PaymentStatus,
    ServiceEntitlement,
    TaxApplication,
    TopupIntent,
)
from app.models.billing_contract import (
    CadenceAlignment,
    CollectionTiming,
    EndOfMonthRule,
    IntervalUnit,
    ProrationPolicy,
    RateBasis,
)
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    Subscription,
    SubscriptionAddOn,
)
from app.models.network_monitoring import CustomerOutageInterval
from app.models.service_period_purchase import (
    PrepaidPeriodPurchase,
    PrepaidPeriodPurchasePeriod,
    PrepaidPeriodPurchaseStatus,
)
from app.schemas.billing import InvoiceCreate, SystemInvoiceLineCreate
from app.services.billing._common import lock_account
from app.services.billing.account_credit import (
    AccountCreditApplicationError,
    AccountCreditApplications,
)
from app.services.billing.cadence import BillingCadence, service_period
from app.services.billing.invoices import (
    InvoiceIssuanceInput,
    InvoiceLines,
    InvoiceOwnerError,
    Invoices,
)
from app.services.billing.payments import (
    Payments,
    finalize_invoice_application_for_owner,
)
from app.services.common import coerce_uuid, round_money
from app.services.customer_financial_position import get_customer_financial_position
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.prepaid_service_renewals import (
    PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES,
    PrepaidMonthlyChargeDetail,
    PrepaidSubscriptionSettlementPeriodQuery,
    project_prepaid_billing_anchor_for_invoice,
    resolve_prepaid_monthly_charge_detail,
    resolve_prepaid_subscription_settlement_period,
)
from app.services.service_period_policy import (
    PrepaidPeriodPurchasePolicy,
    resolve_prepaid_period_purchase_policy,
)
from app.services.topup_intents import (
    COMPLETION_SCOPE,
    CompleteTopupIntentCommand,
    TopupIntentCompletionSource,
    stage_topup_intent_completion,
)
from app.timezone import APP_TIMEZONE_NAME

_OWNER = "financial.prepaid_period_purchases"
_POLICY_VERSION = 1
_QUOTE_TTL = timedelta(minutes=30)
_CREATE_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="prepaid service-period purchase quote persistence",
    name="create_prepaid_period_purchase",
)
_SETTLE_VERIFIED_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="verified prepaid service-period purchase settlement",
    name="settle_verified_prepaid_period_purchase",
)


class PrepaidPeriodPurchaseError(DomainError, ValueError):
    """Stable fail-closed rejection from the period-purchase owner."""


def _error(suffix: str, message: str, **details: object) -> PrepaidPeriodPurchaseError:
    return PrepaidPeriodPurchaseError(
        code=f"{_OWNER}.{suffix}", message=message, details=details
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _policy(db: Session) -> PrepaidPeriodPurchasePolicy:
    try:
        return resolve_prepaid_period_purchase_policy(db)
    except (TypeError, ValueError) as exc:
        raise _error(
            "configuration_invalid", "Purchase period limit is invalid."
        ) from exc


@dataclass(frozen=True, slots=True)
class PrepaidPeriodQuoteLine:
    ordinal: int
    starts_at: datetime
    ends_at: datetime
    unit_price: Decimal
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    tax_rate_id: UUID | None
    tax_application: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PrepaidPeriodPurchaseQuote:
    account_id: UUID
    subscription_id: UUID
    period_count: int
    currency: str
    coverage_starts_at: datetime
    coverage_ends_at: datetime
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    periods: tuple[PrepaidPeriodQuoteLine, ...]
    fingerprint: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CreatePrepaidPeriodPurchaseCommand:
    account_id: UUID
    subscription_id: UUID
    period_count: int
    expected_fingerprint: str
    idempotency_key: str
    created_by: str
    effective_at: datetime


@dataclass(frozen=True, slots=True)
class SettlePrepaidPeriodPurchaseCommand:
    purchase_id: UUID
    payment_id: UUID
    effective_at: datetime
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class SettleVerifiedPrepaidPeriodPurchaseCommand:
    intent_id: UUID
    provider_id: UUID
    external_transaction_id: str
    amount: Decimal
    provider_fee: Decimal
    currency: str
    effective_at: datetime
    completion_source: TopupIntentCompletionSource = (
        TopupIntentCompletionSource.provider_webhook
    )


@dataclass(frozen=True, slots=True)
class PrepaidPeriodPurchaseSettlement:
    purchase_id: UUID
    payment_id: UUID
    invoice_ids: tuple[UUID, ...]
    entitlement_ids: tuple[UUID, ...]
    coverage_ends_at: datetime
    replayed: bool


def _monthly_cadence() -> BillingCadence:
    return BillingCadence(
        rate_basis=RateBasis.fixed_per_service_period,
        rate_unit=IntervalUnit.month,
        rate_quantity=Decimal("1"),
        service_interval_unit=IntervalUnit.month,
        service_interval_count=1,
        invoice_interval_unit=IntervalUnit.month,
        invoice_interval_count=1,
        collection_timing=CollectionTiming.advance,
        alignment=CadenceAlignment.contract_anniversary,
        timezone_name=APP_TIMEZONE_NAME,
        end_of_month_rule=EndOfMonthRule.clamp_to_month_end,
        proration_policy=ProrationPolicy.none,
    )


def _line_fingerprint(
    *,
    ordinal: int,
    starts_at: datetime,
    ends_at: datetime,
    charge: PrepaidMonthlyChargeDetail,
) -> str:
    payload = {
        "ordinal": ordinal,
        "starts_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat(),
        "unit_price": str(charge.unit_price),
        "subtotal": str(charge.subtotal),
        "tax_total": str(charge.tax_total),
        "total": str(charge.total),
        "currency": charge.currency,
        "tax_rate_id": str(charge.tax_rate_id) if charge.tax_rate_id else None,
        "tax_application": charge.tax_application.value,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _quote_fingerprint(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _eligible_subscription(
    db: Session, *, account_id: UUID, subscription_id: UUID, effective_at: datetime
) -> Subscription:
    subscription = db.get(Subscription, subscription_id)
    if subscription is None or subscription.subscriber_id != account_id:
        raise _error("subscription_not_found", "Subscription was not found.")
    if subscription.billing_mode is not BillingMode.prepaid:
        raise _error(
            "billing_mode_ineligible", "Only prepaid service can be purchased."
        )
    if subscription.status not in PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES:
        raise _error(
            "subscription_ineligible", "Subscription is not eligible for renewal."
        )
    position = get_customer_financial_position(db, account_id)
    if position.open_invoice_balance > Decimal("0.00"):
        raise _error(
            "open_debt",
            "Existing invoice debt must be cleared before buying future service periods.",
        )
    open_outage = db.scalar(
        select(CustomerOutageInterval.id).where(
            CustomerOutageInterval.subscription_id == subscription_id,
            CustomerOutageInterval.state == "confirmed_unavailable",
            CustomerOutageInterval.ended_at.is_(None),
        )
    )
    if open_outage is not None:
        raise _error(
            "active_outage",
            "Service periods cannot be purchased while this service has an active outage.",
        )
    active_add_on = db.scalar(
        select(SubscriptionAddOn.id).where(
            SubscriptionAddOn.subscription_id == subscription_id,
            or_(
                SubscriptionAddOn.start_at.is_(None),
                SubscriptionAddOn.start_at <= effective_at,
            ),
            or_(
                SubscriptionAddOn.end_at.is_(None),
                SubscriptionAddOn.end_at > effective_at,
            ),
        )
    )
    if active_add_on is not None:
        raise _error(
            "recurring_add_on_unsupported",
            "Service-period purchase is base-subscription only; recurring add-ons require review.",
        )
    return subscription


def preview_prepaid_period_purchase(
    db: Session,
    *,
    account_id: object,
    subscription_id: object,
    period_count: int,
    effective_at: datetime,
) -> PrepaidPeriodPurchaseQuote:
    policy = _policy(db)
    if not policy.enabled:
        raise _error("feature_disabled", "Service-period purchase is not enabled.")
    maximum = policy.max_months
    if isinstance(period_count, bool) or period_count < 1 or period_count > maximum:
        raise _error(
            "period_count_invalid",
            f"Choose between 1 and {maximum} monthly service periods.",
        )
    account_uuid = coerce_uuid(account_id)
    subscription_uuid = coerce_uuid(subscription_id)
    observed_at = _utc(effective_at)
    subscription = _eligible_subscription(
        db,
        account_id=account_uuid,
        subscription_id=subscription_uuid,
        effective_at=observed_at,
    )
    first_charge = resolve_prepaid_monthly_charge_detail(db, subscription, observed_at)
    if first_charge is None or first_charge.billing_cycle is not BillingCycle.monthly:
        raise _error(
            "monthly_price_unavailable",
            "The subscription does not have one canonical monthly base price.",
        )
    first = resolve_prepaid_subscription_settlement_period(
        db,
        PrepaidSubscriptionSettlementPeriodQuery(
            subscription_id=subscription.id,
            account_id=account_uuid,
            effective_at=observed_at,
            billing_cycle=BillingCycle.monthly,
        ),
    ).period
    cadence = _monthly_cadence()
    periods: list[PrepaidPeriodQuoteLine] = []
    for index in range(period_count):
        interval = service_period(
            cadence=cadence,
            contract_start=first.starts_at,
            index=index,
        )
        starts_at = interval.starts_at.astimezone(UTC)
        ends_at = interval.ends_at.astimezone(UTC)
        charge = resolve_prepaid_monthly_charge_detail(db, subscription, starts_at)
        if (
            charge is None
            or charge.billing_cycle is not BillingCycle.monthly
            or charge.currency != first_charge.currency
        ):
            raise _error(
                "price_changed", "Monthly price evidence changed during quote creation."
            )
        fingerprint = _line_fingerprint(
            ordinal=index + 1, starts_at=starts_at, ends_at=ends_at, charge=charge
        )
        periods.append(
            PrepaidPeriodQuoteLine(
                ordinal=index + 1,
                starts_at=starts_at,
                ends_at=ends_at,
                unit_price=charge.unit_price,
                subtotal=charge.subtotal,
                tax_total=charge.tax_total,
                total=charge.total,
                tax_rate_id=charge.tax_rate_id,
                tax_application=charge.tax_application.value,
                fingerprint=fingerprint,
            )
        )
    subtotal = round_money(sum((row.subtotal for row in periods), Decimal("0.00")))
    tax_total = round_money(sum((row.tax_total for row in periods), Decimal("0.00")))
    total = round_money(sum((row.total for row in periods), Decimal("0.00")))
    payload: dict[str, object] = {
        "account_id": str(account_uuid),
        "subscription_id": str(subscription_uuid),
        "period_count": period_count,
        "currency": first_charge.currency,
        "subtotal": str(subtotal),
        "tax_total": str(tax_total),
        "total": str(total),
        "periods": [row.fingerprint for row in periods],
        "policy_version": _POLICY_VERSION,
    }
    return PrepaidPeriodPurchaseQuote(
        account_id=account_uuid,
        subscription_id=subscription_uuid,
        period_count=period_count,
        currency=first_charge.currency,
        coverage_starts_at=periods[0].starts_at,
        coverage_ends_at=periods[-1].ends_at,
        subtotal=subtotal,
        tax_total=tax_total,
        total=total,
        periods=tuple(periods),
        fingerprint=_quote_fingerprint(payload),
        expires_at=observed_at + _QUOTE_TTL,
    )


def create_prepaid_period_purchase(
    db: Session,
    command: CreatePrepaidPeriodPurchaseCommand,
    *,
    context: CommandContext,
) -> PrepaidPeriodPurchase:
    return execute_owner_command(
        db,
        definition=_CREATE_COMMAND,
        context=context,
        operation=lambda: _stage_prepaid_period_purchase(db, command),
    )


def _stage_prepaid_period_purchase(
    db: Session, command: CreatePrepaidPeriodPurchaseCommand
) -> PrepaidPeriodPurchase:
    key = command.idempotency_key.strip()
    actor = command.created_by.strip()
    if not key or not actor:
        raise _error("command_invalid", "Idempotency and actor evidence are required.")
    lock_account(db, str(command.account_id))
    existing = db.scalar(
        select(PrepaidPeriodPurchase).where(
            PrepaidPeriodPurchase.account_id == command.account_id,
            PrepaidPeriodPurchase.idempotency_key == key,
        )
    )
    if existing is not None:
        if (
            existing.subscription_id != command.subscription_id
            or existing.period_count != command.period_count
            or existing.preview_fingerprint != command.expected_fingerprint
        ):
            raise _error(
                "idempotency_conflict", "Purchase key already names another quote."
            )
        return existing
    quote = preview_prepaid_period_purchase(
        db,
        account_id=command.account_id,
        subscription_id=command.subscription_id,
        period_count=command.period_count,
        effective_at=command.effective_at,
    )
    if quote.fingerprint != command.expected_fingerprint:
        raise _error("stale_quote", "Service-period quote changed before confirmation.")
    purchase = PrepaidPeriodPurchase(
        account_id=quote.account_id,
        subscription_id=quote.subscription_id,
        status=PrepaidPeriodPurchaseStatus.quoted,
        period_count=quote.period_count,
        currency=quote.currency,
        coverage_starts_at=quote.coverage_starts_at,
        coverage_ends_at=quote.coverage_ends_at,
        subtotal=quote.subtotal,
        tax_total=quote.tax_total,
        total=quote.total,
        preview_fingerprint=quote.fingerprint,
        policy_version=_POLICY_VERSION,
        policy_snapshot={
            "max_periods": _policy(db).max_months,
            "vat_rounding": "per_period",
            "debt_policy": "any_open_invoice_blocks",
            "add_on_policy": "base_subscription_only",
        },
        idempotency_key=key,
        created_by=actor,
        expires_at=quote.expires_at,
    )
    db.add(purchase)
    db.flush()
    for row in quote.periods:
        db.add(
            PrepaidPeriodPurchasePeriod(
                purchase_id=purchase.id,
                subscription_id=purchase.subscription_id,
                ordinal=row.ordinal,
                starts_at=row.starts_at,
                ends_at=row.ends_at,
                unit_price=row.unit_price,
                subtotal=row.subtotal,
                tax_total=row.tax_total,
                total=row.total,
                tax_rate_id=row.tax_rate_id,
                tax_application=row.tax_application,
                preview_fingerprint=row.fingerprint,
            )
        )
    db.flush()
    db.refresh(purchase)
    return purchase


def stage_verified_prepaid_period_purchase(
    db: Session,
    command: SettleVerifiedPrepaidPeriodPurchaseCommand,
    *,
    context: CommandContext,
) -> PrepaidPeriodPurchaseSettlement:
    intent = db.get(TopupIntent, command.intent_id)
    if (
        intent is None
        or intent.purpose != "prepaid_period_purchase"
        or intent.account_id is None
    ):
        raise _error("intent_invalid", "Payment intent is not a period purchase.")
    purchase = db.scalar(
        select(PrepaidPeriodPurchase).where(
            PrepaidPeriodPurchase.topup_intent_id == intent.id
        )
    )
    provider = db.get(PaymentProvider, command.provider_id)
    external_id = command.external_transaction_id.strip()
    amount = round_money(command.amount)
    fee = round_money(command.provider_fee)
    currency = command.currency.strip().upper()
    if (
        purchase is None
        or provider is None
        or not external_id
        or amount != round_money(purchase.total)
        or currency != purchase.currency
    ):
        raise _error(
            "provider_evidence_mismatch",
            "Verified provider evidence does not match the period purchase.",
        )
    payment_result = Payments.stage_verified_provider_settlement(
        db,
        account_id=purchase.account_id,
        provider_id=provider.id,
        external_id=external_id,
        gross_amount=amount,
        provider_fee=fee,
        # Gateway fees are a merchant expense. The customer's full confirmed
        # charge funds the service periods, matching existing typed top-up
        # behavior and preventing a fee-sized invoice shortfall.
        net_amount=round_money(purchase.total),
        currency=currency,
        memo=f"Prepaid service-period purchase {purchase.id}",
        paid_at=_utc(command.effective_at),
    )
    settlement = settle_prepaid_period_purchase(
        db,
        SettlePrepaidPeriodPurchaseCommand(
            purchase_id=purchase.id,
            payment_id=payment_result.payment.id,
            effective_at=command.effective_at,
            evidence_ref=f"provider:{provider.id}:{external_id}",
        ),
    )
    stage_topup_intent_completion(
        db,
        CompleteTopupIntentCommand(
            intent_id=intent.id,
            payment_id=payment_result.payment.id,
            source=command.completion_source,
        ),
        context=CommandContext.system(
            actor=context.actor,
            scope=COMPLETION_SCOPE,
            reason="Complete prepaid service-period purchase intent",
            correlation_id=context.correlation_id,
            causation_id=context.command_id,
        ),
    )
    return settlement


def settle_verified_prepaid_period_purchase(
    db: Session,
    command: SettleVerifiedPrepaidPeriodPurchaseCommand,
    *,
    context: CommandContext,
) -> PrepaidPeriodPurchaseSettlement:
    return execute_owner_command(
        db,
        definition=_SETTLE_VERIFIED_COMMAND,
        context=context,
        operation=lambda: stage_verified_prepaid_period_purchase(
            db, command, context=context
        ),
    )


def settle_prepaid_period_purchase(
    db: Session, command: SettlePrepaidPeriodPurchaseCommand
) -> PrepaidPeriodPurchaseSettlement:
    evidence = command.evidence_ref.strip()
    if not evidence:
        raise _error("evidence_missing", "Provider settlement evidence is required.")
    purchase = db.get(PrepaidPeriodPurchase, command.purchase_id)
    if purchase is None:
        raise _error("purchase_not_found", "Service-period purchase was not found.")
    lock_account(db, str(purchase.account_id))
    db.refresh(purchase)
    rows = list(
        db.scalars(
            select(PrepaidPeriodPurchasePeriod)
            .where(PrepaidPeriodPurchasePeriod.purchase_id == purchase.id)
            .order_by(PrepaidPeriodPurchasePeriod.ordinal)
        ).all()
    )
    if purchase.status is PrepaidPeriodPurchaseStatus.completed:
        if purchase.payment_id != command.payment_id or any(
            row.invoice_id is None or row.entitlement_id is None for row in rows
        ):
            raise _error(
                "idempotency_conflict", "Completed purchase evidence is incomplete."
            )
        return PrepaidPeriodPurchaseSettlement(
            purchase_id=purchase.id,
            payment_id=command.payment_id,
            invoice_ids=tuple(row.invoice_id for row in rows if row.invoice_id),
            entitlement_ids=tuple(
                row.entitlement_id for row in rows if row.entitlement_id
            ),
            coverage_ends_at=purchase.coverage_ends_at,
            replayed=True,
        )
    if purchase.status not in {
        PrepaidPeriodPurchaseStatus.quoted,
        PrepaidPeriodPurchaseStatus.payment_pending,
    }:
        raise _error(
            "purchase_ineligible", "Purchase cannot be settled in its current state."
        )
    observed_at = _utc(command.effective_at)
    if purchase.expires_at < observed_at:
        purchase.status = PrepaidPeriodPurchaseStatus.expired
        raise _error("purchase_expired", "Service-period quote has expired.")
    if len(rows) != purchase.period_count or not rows:
        raise _error("purchase_incomplete", "Purchase period evidence is incomplete.")
    if any(left.ends_at != right.starts_at for left, right in zip(rows, rows[1:])):
        raise _error("purchase_noncontiguous", "Purchase periods are not contiguous.")
    payment = db.get(Payment, command.payment_id)
    if (
        payment is None
        or not payment.is_active
        or payment.status is not PaymentStatus.succeeded
        or payment.account_id != purchase.account_id
        or (payment.currency or "NGN").upper() != purchase.currency
        or round_money(payment.amount) != round_money(purchase.total)
        or payment.allocations
    ):
        raise _error(
            "payment_mismatch",
            "Verified payment must exactly and exclusively fund this purchase.",
        )
    invoice_ids: list[UUID] = []
    entitlement_ids: list[UUID] = []
    try:
        for row in rows:
            invoice = Invoices.stage_system_invoice_for_owner(
                db,
                InvoiceCreate(
                    account_id=purchase.account_id,
                    status=InvoiceStatus.draft,
                    currency=purchase.currency,
                    subtotal=row.subtotal,
                    tax_total=row.tax_total,
                    total=row.total,
                    balance_due=row.total,
                    billing_period_start=row.starts_at,
                    billing_period_end=row.ends_at,
                    memo=f"Prepaid service period {row.ordinal} of {purchase.period_count}",
                ),
                reason="prepaid_period_purchase",
            )
            line = InvoiceLines.stage_system_line_for_owner(
                db,
                SystemInvoiceLineCreate(
                    invoice_id=invoice.id,
                    subscription_id=purchase.subscription_id,
                    description=f"Prepaid service period {row.ordinal} of {purchase.period_count}",
                    quantity=Decimal("1.000"),
                    unit_price=row.unit_price,
                    amount=row.unit_price,
                    tax_rate_id=row.tax_rate_id,
                    tax_application=TaxApplication(row.tax_application),
                    metadata_={
                        "kind": "base_subscription",
                        "billing_period_start": row.starts_at.isoformat(),
                        "billing_period_end": row.ends_at.isoformat(),
                        "prepaid_period_purchase_id": str(purchase.id),
                        "prepaid_period_purchase_ordinal": row.ordinal,
                        "purchase_period_fingerprint": row.preview_fingerprint,
                        "settlement_evidence_ref": evidence,
                    },
                    billing_line_key=f"prepaid-period-purchase:{purchase.id}:{row.ordinal}",
                ),
                reason="prepaid_period_purchase",
            )
            invoice.metadata_ = {
                **(invoice.metadata_ or {}),
                "renewal_period_authoritative": True,
                "renewal_totals_authoritative": True,
                "prepaid_period_purchase_id": str(purchase.id),
            }
            db.flush()
            Invoices.issue_draft_for_owner(
                db,
                str(invoice.id),
                issuance=InvoiceIssuanceInput(
                    issued_at=observed_at,
                    due_at=observed_at,
                    due_date_basis=InvoiceDueDateBasis.prepaid_service_period,
                    due_date_basis_ref=f"prepaid_period_purchases:{purchase.id}:{row.ordinal}",
                    due_date_policy_version="prepaid-period-purchases-v1",
                    reason="prepaid_period_purchase",
                ),
                apply_available_credit=False,
            )
            AccountCreditApplications.apply_invoice_from_selected_payment_fully(
                db,
                invoice,
                payment_id=payment.id,
                expected_amount=row.total,
            )
            finalize_invoice_application_for_owner(
                db, invoice, effective_at=observed_at
            )
            entitlement = db.scalar(
                select(ServiceEntitlement).where(
                    ServiceEntitlement.source_invoice_line_id == line.id
                )
            )
            if entitlement is None:
                raise _error(
                    "entitlement_missing",
                    "Paid purchase invoice did not create service entitlement evidence.",
                    invoice_id=str(invoice.id),
                )
            project_prepaid_billing_anchor_for_invoice(
                db,
                invoice,
                evidence_ref=f"prepaid_period_purchases:{purchase.id}:{row.ordinal}",
            )
            row.invoice_id = invoice.id
            row.invoice_line_id = line.id
            row.entitlement_id = entitlement.id
            invoice_ids.append(invoice.id)
            entitlement_ids.append(entitlement.id)
    except (InvoiceOwnerError, AccountCreditApplicationError) as exc:
        raise _error(
            "settlement_rejected",
            "Purchase settlement did not produce exactly paid period invoices.",
            participant_error=getattr(exc, "code", type(exc).__name__),
        ) from exc
    purchase.payment_id = payment.id
    purchase.status = PrepaidPeriodPurchaseStatus.completed
    purchase.completed_at = observed_at
    db.flush()
    return PrepaidPeriodPurchaseSettlement(
        purchase_id=purchase.id,
        payment_id=payment.id,
        invoice_ids=tuple(invoice_ids),
        entitlement_ids=tuple(entitlement_ids),
        coverage_ends_at=purchase.coverage_ends_at,
        replayed=False,
    )


__all__ = [
    "CreatePrepaidPeriodPurchaseCommand",
    "PrepaidPeriodPurchaseError",
    "PrepaidPeriodPurchaseQuote",
    "PrepaidPeriodPurchaseSettlement",
    "SettlePrepaidPeriodPurchaseCommand",
    "SettleVerifiedPrepaidPeriodPurchaseCommand",
    "create_prepaid_period_purchase",
    "preview_prepaid_period_purchase",
    "settle_prepaid_period_purchase",
    "settle_verified_prepaid_period_purchase",
    "stage_verified_prepaid_period_purchase",
]
