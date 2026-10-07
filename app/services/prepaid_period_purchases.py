"""Owner for quoted multi-period prepaid purchases and exact settlement."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TypedDict
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    InvoiceDueDateBasis,
    InvoiceStatus,
    Payment,
    PaymentProvider,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
    TaxApplication,
    TaxRate,
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
from app.models.service_period_purchase import (
    PrepaidPeriodPurchase,
    PrepaidPeriodPurchasePeriod,
    PrepaidPeriodPurchaseStatus,
)
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.models.subscription_lifecycle_schedule import (
    SubscriptionLifecycleSchedule,
    SubscriptionLifecycleScheduleStatus,
)
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import InvoiceCreate, SystemInvoiceLineCreate
from app.services.audit import AuditEvents
from app.services.billing._common import lock_account
from app.services.billing.account_credit import (
    AccountCreditApplicationError,
    AccountCreditApplications,
)
from app.services.billing.cadence import BillingCadence, service_period
from app.services.billing.invoices import (
    InvoiceIssuanceInput,
    InvoiceLines,
    InvoiceLineTaxSnapshot,
    InvoiceOwnerError,
    Invoices,
)
from app.services.billing.payments import (
    Payments,
    finalize_invoice_application_for_owner,
)
from app.services.billing_tax_resolution import resolve_subscription_taxes
from app.services.common import coerce_uuid, round_money
from app.services.customer_financial_position import get_customer_financial_position
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    execute_owner_savepoint,
)
from app.services.prepaid_service_renewals import (
    PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES,
    PrepaidMonthlyChargeDetail,
    PrepaidSubscriptionSettlementPeriodQuery,
    project_prepaid_billing_anchor_for_invoice,
    resolve_prepaid_monthly_charge_detail,
    resolve_prepaid_subscription_settlement_period,
)
from app.services.purchase_payment_recovery_state import (
    PurchasePaymentRecoveryCommand,
    ResolveUnpaidPurchaseIntentCommand,
    stage_purchase_payment_recovery,
    stage_unpaid_purchase_intent_resolution,
    unpaid_purchase_intent_can_close,
)
from app.services.purchased_service_coverage import (
    PurchasedCoverage,
    PurchasedCoverageQuery,
    resolve_purchased_coverage,
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
_POLICY_VERSION = 2
_QUOTE_TTL = timedelta(minutes=30)
PURCHASE_REPAIR_SCOPE = "billing:prepaid_reconciliation:repair"
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
_RETRY_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="reviewed prepaid purchase receipt recovery",
    name="retry_prepaid_period_purchase_settlement",
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


class PurchaseTaxFacts(TypedDict):
    source: str
    customer_tax_policy_version: int
    rate_id: str | None
    code: str | None
    rate: str | None
    is_active: bool | None


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
    tax_snapshot: PurchaseTaxFacts | None = None


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
    provider_paid_at: datetime | None = None


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
    provider_paid_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PrepaidPeriodPurchaseSettlement:
    purchase_id: UUID
    payment_id: UUID | None
    invoice_ids: tuple[UUID, ...]
    entitlement_ids: tuple[UUID, ...]
    coverage_ends_at: datetime
    replayed: bool
    status: PrepaidPeriodPurchaseStatus = PrepaidPeriodPurchaseStatus.completed
    failure_code: str | None = None


class PurchaseRecoveryAction(StrEnum):
    await_provider = "await_provider"
    close_unpaid_checkout = "close_unpaid_checkout"
    start_new_checkout = "start_new_checkout"
    retry_settlement = "retry_settlement"
    refund_or_provider_review = "refund_or_provider_review"
    resolve_blocker = "resolve_blocker"
    complete = "complete"


@dataclass(frozen=True, slots=True)
class PurchaseReceiptSummary:
    payment_id: UUID
    amount: Decimal
    refunded_amount: Decimal
    currency: str
    status: PaymentStatus


@dataclass(frozen=True, slots=True)
class PurchaseRecoveryPreview:
    purchase_id: UUID
    payment_id: UUID | None
    status: PrepaidPeriodPurchaseStatus
    action: PurchaseRecoveryAction
    failure_code: str | None
    fingerprint: str
    receipts: tuple[PurchaseReceiptSummary, ...]


def preview_purchase_recovery(
    db: Session, purchase_id: UUID
) -> PurchaseRecoveryPreview:
    purchase = db.get(PrepaidPeriodPurchase, purchase_id)
    if purchase is None:
        raise _error("purchase_not_found", "Purchase was not found.")
    payment = db.get(Payment, purchase.payment_id) if purchase.payment_id else None
    receipts = tuple(
        PurchaseReceiptSummary(
            payment_id=row.id,
            amount=row.amount,
            refunded_amount=row.refunded_amount,
            currency=row.currency,
            status=row.status,
        )
        for row in db.scalars(
            select(Payment)
            .where(
                Payment.reserved_for_purchase_id == purchase.id,
            )
            .order_by(Payment.created_at, Payment.id)
        ).all()
    )
    action = PurchaseRecoveryAction.await_provider
    reason = purchase.failure_code
    current_quote = None
    if payment is not None and payment.status is PaymentStatus.succeeded:
        try:
            quote = preview_prepaid_period_purchase(
                db,
                account_id=purchase.account_id,
                subscription_id=purchase.subscription_id,
                period_count=purchase.period_count,
                effective_at=_utc(purchase.created_at),
                for_existing_purchase_id=purchase.id,
            )
            current_quote = quote.fingerprint
            paid_at = purchase.verified_paid_at or payment.created_at
            if (
                quote.fingerprint == purchase.preview_fingerprint
                and paid_at is not None
                and _utc(paid_at) <= _utc(purchase.expires_at)
                and _utc(paid_at) >= _utc(purchase.created_at)
                and payment.currency == purchase.currency
                and round_money(payment.amount) == round_money(purchase.total)
                and not payment.refunds
                and not payment.allocations
            ):
                action = PurchaseRecoveryAction.retry_settlement
            else:
                action = PurchaseRecoveryAction.refund_or_provider_review
        except PrepaidPeriodPurchaseError as exc:
            action = PurchaseRecoveryAction.resolve_blocker
            reason = exc.code
    if purchase.status is PrepaidPeriodPurchaseStatus.completed:
        action = PurchaseRecoveryAction.complete
    if (
        purchase.status
        in {
            PrepaidPeriodPurchaseStatus.failed,
            PrepaidPeriodPurchaseStatus.expired,
            PrepaidPeriodPurchaseStatus.canceled,
        }
        and not receipts
    ):
        action = PurchaseRecoveryAction.start_new_checkout
    elif unpaid_purchase_intent_can_close(db, purchase):
        action = PurchaseRecoveryAction.close_unpaid_checkout
    if (
        db.scalar(
            select(Payment.id)
            .where(
                Payment.reserved_for_purchase_id == purchase.id,
                Payment.id != purchase.payment_id,
                Payment.status.in_(
                    [PaymentStatus.succeeded, PaymentStatus.partially_refunded]
                ),
            )
            .limit(1)
        )
        is not None
    ):
        action = PurchaseRecoveryAction.refund_or_provider_review
    payload: dict[str, object] = {
        "purchase_id": str(purchase.id),
        "payment_id": str(purchase.payment_id),
        "status": purchase.status.value,
        "action": action,
        "reason": reason,
        "quote": current_quote,
        "payment_status": payment.status.value if payment else None,
        "verified_paid_at": str(purchase.verified_paid_at),
        "receipts": [
            {
                "id": str(row.payment_id),
                "amount": str(row.amount),
                "refunded": str(row.refunded_amount),
                "currency": row.currency,
                "status": row.status.value,
            }
            for row in receipts
        ],
    }
    return PurchaseRecoveryPreview(
        purchase.id,
        purchase.payment_id,
        purchase.status,
        action,
        reason,
        _quote_fingerprint(payload),
        receipts,
    )


@dataclass(frozen=True, slots=True)
class RetryPurchaseSettlementCommand:
    purchase_id: UUID
    expected_fingerprint: str
    effective_at: datetime
    permission_granted: bool
    actor_system_user_id: UUID


def retry_purchase_settlement(
    db: Session, command: RetryPurchaseSettlementCommand, *, context: CommandContext
) -> PrepaidPeriodPurchaseSettlement:
    def operation() -> PrepaidPeriodPurchaseSettlement:
        if not command.permission_granted or context.scope != PURCHASE_REPAIR_SCOPE:
            raise _error(
                "repair_permission_required",
                "Prepaid reconciliation permission is required.",
            )
        from app.services.outage_compensation import require_time_credit_staff

        if context.actor not in {
            f"staff:{command.actor_system_user_id}",
            f"user:{command.actor_system_user_id}",
        }:
            raise _error(
                "repair_permission_required", "Named staff repair evidence is required."
            )
        require_time_credit_staff(
            db, command.actor_system_user_id, PURCHASE_REPAIR_SCOPE
        )
        key = (context.idempotency_key or "").strip()
        if not key or not context.reason.strip():
            raise _error(
                "command_invalid", "A recovery idempotency key and reason are required."
            )
        purchase = db.get(PrepaidPeriodPurchase, command.purchase_id)
        if purchase is None:
            raise _error("purchase_not_found", "Purchase was not found.")
        lock_account(db, str(purchase.account_id))
        db.refresh(purchase, with_for_update=True)
        receipts = (purchase.policy_snapshot or {}).get("recovery_receipts", {})
        previous_fingerprint = receipts.get(key)
        if (
            previous_fingerprint is not None
            and previous_fingerprint != command.expected_fingerprint
        ):
            raise _error(
                "idempotency_conflict",
                "Recovery key names a different reviewed preview.",
            )
        if (
            previous_fingerprint == command.expected_fingerprint
            and purchase.status is PrepaidPeriodPurchaseStatus.failed
        ):
            return PrepaidPeriodPurchaseSettlement(
                purchase_id=purchase.id,
                payment_id=None,
                invoice_ids=(),
                entitlement_ids=(),
                coverage_ends_at=purchase.coverage_ends_at,
                replayed=True,
                status=purchase.status,
                failure_code=purchase.failure_code,
            )
        preview = preview_purchase_recovery(db, purchase.id)
        completed_replay = (
            previous_fingerprint == command.expected_fingerprint
            and purchase.status is PrepaidPeriodPurchaseStatus.completed
        )
        if not completed_replay and preview.fingerprint != command.expected_fingerprint:
            raise _error("stale_quote", "Recovery evidence changed; review it again.")
        if preview.action is PurchaseRecoveryAction.close_unpaid_checkout:
            assert purchase.topup_intent_id is not None
            if not stage_unpaid_purchase_intent_resolution(
                db,
                ResolveUnpaidPurchaseIntentCommand(intent_id=purchase.topup_intent_id),
            ):
                raise _error(
                    "recovery_evidence_invalid", "Unpaid provider evidence changed."
                )
            purchase.policy_snapshot = {
                **(purchase.policy_snapshot or {}),
                "recovery_receipts": {**receipts, key: command.expected_fingerprint},
            }
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=AuditActorType.user,
                    actor_id=str(command.actor_system_user_id),
                    actor_label=context.actor,
                    action="prepaid_period_purchase.unpaid_checkout_closed",
                    entity_type="prepaid_period_purchase",
                    entity_id=str(purchase.id),
                    metadata_={
                        "reason": context.reason,
                        "preview_fingerprint": command.expected_fingerprint,
                    },
                ),
            )
            return PrepaidPeriodPurchaseSettlement(
                purchase_id=purchase.id,
                payment_id=None,
                invoice_ids=(),
                entitlement_ids=(),
                coverage_ends_at=purchase.coverage_ends_at,
                replayed=False,
                status=purchase.status,
                failure_code=purchase.failure_code,
            )
        payment = db.get(Payment, purchase.payment_id) if purchase.payment_id else None
        if (
            payment is None
            or payment.provider_id is None
            or not payment.external_id
            or purchase.topup_intent_id is None
            or preview.action not in {"retry_settlement", "complete"}
        ):
            raise _error(
                "recovery_evidence_invalid",
                "This receipt requires provider or refund review.",
            )
        result = stage_verified_prepaid_period_purchase(
            db,
            SettleVerifiedPrepaidPeriodPurchaseCommand(
                intent_id=purchase.topup_intent_id,
                provider_id=payment.provider_id,
                external_transaction_id=payment.external_id,
                amount=payment.amount,
                provider_fee=payment.provider_fee,
                currency=payment.currency,
                effective_at=command.effective_at,
                provider_paid_at=purchase.verified_paid_at or payment.created_at,
            ),
            context=context,
        )
        if not completed_replay:
            if result.status is PrepaidPeriodPurchaseStatus.completed:
                purchase.policy_snapshot = {
                    **(purchase.policy_snapshot or {}),
                    "recovery_receipts": {
                        **receipts,
                        key: command.expected_fingerprint,
                    },
                }
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=AuditActorType.user,
                    actor_id=str(command.actor_system_user_id),
                    actor_label=context.actor,
                    action="prepaid_period_purchase.receipt_recovery",
                    entity_type="prepaid_period_purchase",
                    entity_id=str(purchase.id),
                    metadata_={
                        "reason": context.reason,
                        "preview_fingerprint": command.expected_fingerprint,
                        "payment_id": str(result.payment_id),
                        "status": result.status.value,
                        "staff_principal_id": str(command.actor_system_user_id),
                    },
                ),
            )
            db.flush()
        return result

    return execute_owner_command(
        db, definition=_RETRY_COMMAND, context=context, operation=operation
    )


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
    tax_snapshot: PurchaseTaxFacts | None = None,
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
        "tax_snapshot": tax_snapshot,
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
    allowance_id = (
        subscription.offer_version.usage_allowance_id
        if subscription.offer_version is not None
        else subscription.offer.usage_allowance_id
        if subscription.offer
        else None
    )
    if allowance_id is not None:
        raise _error(
            "usage_allowance_unsupported",
            "Metered service requires separate period and quota settlement.",
        )
    pending_change = db.scalar(
        select(SubscriptionChangeRequest.id).where(
            SubscriptionChangeRequest.subscription_id == subscription.id,
            SubscriptionChangeRequest.is_active.is_(True),
            SubscriptionChangeRequest.applied_at.is_(None),
            SubscriptionChangeRequest.status.in_(
                [
                    SubscriptionChangeStatus.pending,
                    SubscriptionChangeStatus.approved,
                ]
            ),
        )
    )
    pending_lifecycle = db.scalar(
        select(SubscriptionLifecycleSchedule.id).where(
            SubscriptionLifecycleSchedule.subscription_id == subscription.id,
            SubscriptionLifecycleSchedule.status.in_(
                [
                    SubscriptionLifecycleScheduleStatus.pending,
                    SubscriptionLifecycleScheduleStatus.processing,
                ]
            ),
        )
    )
    if pending_change is not None or pending_lifecycle is not None:
        raise _error(
            "pending_lifecycle",
            "Resolve pending service changes before purchasing future periods.",
        )
    position = get_customer_financial_position(db, account_id)
    if position.open_invoice_balance > Decimal("0.00"):
        raise _error(
            "open_debt",
            "Existing invoice debt must be cleared before buying future service periods.",
        )
    from app.services.network.customer_outage_accrual import (
        OutagePurchaseAdmissionQuery,
        resolve_outage_purchase_admission,
    )

    outage = resolve_outage_purchase_admission(
        db, OutagePurchaseAdmissionQuery(subscription_id)
    )
    if not outage.allowed:
        raise _error(
            "active_outage",
            "Service periods cannot be purchased until outage recovery is finalized.",
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
    account_id: UUID | str,
    subscription_id: UUID | str,
    period_count: int,
    effective_at: datetime,
    for_existing_purchase_id: UUID | None = None,
) -> PrepaidPeriodPurchaseQuote:
    policy = _policy(db)
    if not policy.enabled:
        existing_purchase = (
            db.get(PrepaidPeriodPurchase, for_existing_purchase_id)
            if for_existing_purchase_id
            else None
        )
        if (
            existing_purchase is None
            or existing_purchase.account_id != coerce_uuid(account_id)
            or existing_purchase.subscription_id != coerce_uuid(subscription_id)
        ):
            raise _error("feature_disabled", "Service-period purchase is not enabled.")
    maximum = policy.max_months
    if (
        isinstance(period_count, bool)
        or not isinstance(period_count, int)
        or period_count < 1
        or period_count > maximum
    ):
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
    if first_charge.currency != "NGN":
        raise _error(
            "currency_unsupported",
            "Service-period purchases currently support NGN only.",
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
    if subscription.end_at is not None:
        raise _error(
            "explicit_end",
            "A service with an explicit end date requires billing review before future purchases.",
        )
    tax_resolution = resolve_subscription_taxes(db, [subscription])[subscription.id]
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
        rate = db.get(TaxRate, charge.tax_rate_id) if charge.tax_rate_id else None
        tax_snapshot: PurchaseTaxFacts = {
            "source": tax_resolution.source.value,
            "customer_tax_policy_version": tax_resolution.customer_tax_policy_version,
            "rate_id": str(charge.tax_rate_id) if charge.tax_rate_id else None,
            "code": rate.code if rate else None,
            "rate": str(rate.rate) if rate else None,
            "is_active": rate.is_active if rate else None,
        }
        fingerprint = _line_fingerprint(
            ordinal=index + 1,
            starts_at=starts_at,
            ends_at=ends_at,
            charge=charge,
            tax_snapshot=tax_snapshot,
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
                tax_snapshot=tax_snapshot,
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
    if not key or len(key) > 120 or not actor:
        raise _error("command_invalid", "Idempotency and actor evidence are required.")
    lock_account(db, str(command.account_id))
    db.scalar(
        select(Subscription)
        .where(Subscription.id == command.subscription_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
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
        if (
            existing.status is PrepaidPeriodPurchaseStatus.failed
            and existing.payment_id is None
            and existing.failure_code
            == "financial.purchase_payment_recovery_state.provider_confirmed_unpaid"
            and db.scalar(
                select(Payment.id)
                .where(Payment.reserved_for_purchase_id == existing.id)
                .limit(1)
            )
            is None
        ):
            raise _error(
                "purchase_closed_unpaid",
                "This verified unpaid checkout is closed. Review a new quote.",
                safe_new_checkout=True,
            )
        if (
            existing.topup_intent_id is None
            and existing.payment_id is None
            and _utc(existing.expires_at) <= _utc(command.effective_at)
        ):
            raise _error(
                "purchase_expired",
                "This unstarted quote expired. Prepare a new quote.",
                checkout_started=False,
            )
        return existing
    live = db.scalar(
        select(PrepaidPeriodPurchase)
        .where(
            PrepaidPeriodPurchase.subscription_id == command.subscription_id,
            PrepaidPeriodPurchase.status.in_(
                [
                    PrepaidPeriodPurchaseStatus.quoted,
                    PrepaidPeriodPurchaseStatus.payment_pending,
                    PrepaidPeriodPurchaseStatus.review_required,
                ]
            ),
        )
        .with_for_update()
    )
    if live is not None and live.topup_intent_id is not None:
        if stage_unpaid_purchase_intent_resolution(
            db, ResolveUnpaidPurchaseIntentCommand(intent_id=live.topup_intent_id)
        ):
            live = None
    if live is not None:
        if (
            live.topup_intent_id is None
            and live.payment_id is None
            and _utc(live.expires_at) < _utc(command.effective_at)
        ):
            live.status = PrepaidPeriodPurchaseStatus.expired
            db.flush()
        else:
            raise _error(
                "checkout_in_progress",
                "This service already has a purchase awaiting payment or review.",
            )
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
            "tax_facts": {str(row.ordinal): row.tax_snapshot for row in quote.periods},
        },
        idempotency_key=key,
        created_by=actor,
        created_at=_utc(command.effective_at),
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
    lock_account(db, str(intent.account_id))
    db.refresh(intent)
    purchase = db.scalar(
        select(PrepaidPeriodPurchase)
        .where(PrepaidPeriodPurchase.topup_intent_id == intent.id)
        .with_for_update()
        .execution_options(populate_existing=True)
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
        or provider.id != intent.provider_id
        or purchase.account_id != intent.account_id
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
        net_amount=amount,
        currency=currency,
        memo=f"Prepaid service-period purchase {purchase.id}",
        paid_at=_utc(command.provider_paid_at or command.effective_at),
        reserved_for_purchase_id=purchase.id,
    )
    if (
        purchase.payment_id is not None
        and purchase.payment_id != payment_result.payment.id
    ):
        purchase.status = PrepaidPeriodPurchaseStatus.review_required
        purchase.failure_code = f"{_OWNER}.additional_capture_review"
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_label=context.actor,
                action="prepaid_period_purchase.additional_capture_held",
                entity_type="prepaid_period_purchase",
                entity_id=str(purchase.id),
                metadata_={
                    "primary_payment_id": str(purchase.payment_id),
                    "held_payment_id": str(payment_result.payment.id),
                },
            ),
        )
        db.flush()
        return PrepaidPeriodPurchaseSettlement(
            purchase_id=purchase.id,
            payment_id=payment_result.payment.id,
            invoice_ids=(),
            entitlement_ids=(),
            coverage_ends_at=purchase.coverage_ends_at,
            replayed=payment_result.idempotent_replay,
            status=purchase.status,
            failure_code=purchase.failure_code,
        )
    purchase.payment_id = payment_result.payment.id
    if command.provider_paid_at is not None:
        if purchase.verified_paid_at is not None and _utc(
            purchase.verified_paid_at
        ) != _utc(command.provider_paid_at):
            raise _error(
                "provider_evidence_mismatch",
                "Provider capture timestamp changed on replay.",
            )
        purchase.verified_paid_at = _utc(command.provider_paid_at)
    db.flush()

    def settle_and_complete() -> PrepaidPeriodPurchaseSettlement:
        settlement = settle_prepaid_period_purchase(
            db,
            SettlePrepaidPeriodPurchaseCommand(
                purchase_id=purchase.id,
                payment_id=payment_result.payment.id,
                effective_at=command.effective_at,
                evidence_ref=f"provider:{provider.id}:{external_id}",
                provider_paid_at=command.provider_paid_at,
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

    try:
        settlement = execute_owner_savepoint(db, settle_and_complete)
    except DomainError as exc:
        db.refresh(purchase)
        purchase.status = PrepaidPeriodPurchaseStatus.review_required
        failure_code = exc.code
        purchase.failure_code = failure_code
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_label=context.actor,
                action="prepaid_period_purchase.settlement_held",
                entity_type="prepaid_period_purchase",
                entity_id=str(purchase.id),
                metadata_={
                    "payment_id": str(payment_result.payment.id),
                    "failure_code": failure_code,
                },
            ),
        )
        db.flush()
        return PrepaidPeriodPurchaseSettlement(
            purchase_id=purchase.id,
            payment_id=payment_result.payment.id,
            invoice_ids=(),
            entitlement_ids=(),
            coverage_ends_at=purchase.coverage_ends_at,
            replayed=payment_result.idempotent_replay,
            status=purchase.status,
            failure_code=failure_code,
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
    db.scalar(
        select(Subscription)
        .where(Subscription.id == purchase.subscription_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
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
        PrepaidPeriodPurchaseStatus.review_required,
    }:
        raise _error(
            "purchase_ineligible", "Purchase cannot be settled in its current state."
        )
    observed_at = _utc(command.effective_at)
    paid_at = (
        _utc(command.provider_paid_at) if command.provider_paid_at else observed_at
    )
    if paid_at > _utc(purchase.expires_at) or paid_at < _utc(purchase.created_at):
        raise _error("purchase_expired", "Service-period quote has expired.")
    if paid_at > observed_at:
        raise _error("payment_time_invalid", "Provider capture time is in the future.")
    # Recompute under the account lock at the original quotation time. A delayed
    # observation never moves the dates the customer bought.
    quote = preview_prepaid_period_purchase(
        db,
        account_id=purchase.account_id,
        subscription_id=purchase.subscription_id,
        period_count=purchase.period_count,
        effective_at=_utc(purchase.created_at),
        for_existing_purchase_id=purchase.id,
    )
    if quote.fingerprint != purchase.preview_fingerprint:
        raise _error(
            "stale_quote",
            "Coverage or price changed after checkout; recorded funds require review.",
        )
    overlap = db.scalar(
        select(ServiceEntitlement.id).where(
            ServiceEntitlement.subscription_id == purchase.subscription_id,
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
            ServiceEntitlement.starts_at < purchase.coverage_ends_at,
            ServiceEntitlement.ends_at > purchase.coverage_starts_at,
        )
    )
    if overlap is not None:
        raise _error(
            "coverage_overlap", "Existing service coverage overlaps this purchase."
        )
    if len(rows) != purchase.period_count or not rows:
        raise _error("purchase_incomplete", "Purchase period evidence is incomplete.")
    if any(
        row.ordinal != expected.ordinal
        or _utc(row.starts_at) != expected.starts_at
        or _utc(row.ends_at) != expected.ends_at
        or row.unit_price != expected.unit_price
        or row.subtotal != expected.subtotal
        or row.tax_total != expected.tax_total
        or row.total != expected.total
        or row.tax_rate_id != expected.tax_rate_id
        or row.tax_application != expected.tax_application
        or row.preview_fingerprint != expected.fingerprint
        for row, expected in zip(rows, quote.periods, strict=True)
    ):
        raise _error(
            "purchase_incomplete",
            "Persisted purchase periods differ from the reviewed quote.",
        )
    if any(left.ends_at != right.starts_at for left, right in zip(rows, rows[1:])):
        raise _error("purchase_noncontiguous", "Purchase periods are not contiguous.")
    payment = db.get(Payment, command.payment_id)
    if (
        db.scalar(
            select(Payment.id)
            .where(
                Payment.reserved_for_purchase_id == purchase.id,
                Payment.id != command.payment_id,
                Payment.status.in_(
                    [PaymentStatus.succeeded, PaymentStatus.partially_refunded]
                ),
            )
            .limit(1)
        )
        is not None
    ):
        raise _error(
            "additional_capture_review",
            "Additional captured receipts require billing review.",
        )
    if (
        payment is None
        or not payment.is_active
        or payment.status is not PaymentStatus.succeeded
        or payment.account_id != purchase.account_id
        or (payment.currency or "NGN").upper() != purchase.currency
        or round_money(payment.amount) != round_money(purchase.total)
        or payment.reserved_for_purchase_id != purchase.id
        or payment.refunds
        or payment.reversal is not None
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
            tax_facts = (
                (purchase.policy_snapshot or {})
                .get("tax_facts", {})
                .get(str(row.ordinal))
            )
            if not tax_facts:
                raise _error(
                    "tax_snapshot_missing",
                    "Purchase predates frozen tax facts; recorded funds require review.",
                )
            tax_snapshot = None
            if row.tax_rate_id is not None:
                code, rate, active = (
                    tax_facts.get("code"),
                    tax_facts.get("rate"),
                    tax_facts.get("is_active"),
                )
                if (
                    not isinstance(rate, str)
                    or not isinstance(active, bool)
                    or (code is not None and not isinstance(code, str))
                ):
                    raise _error(
                        "tax_snapshot_missing",
                        "Frozen tax facts are incomplete; recorded funds require review.",
                    )
                try:
                    frozen_rate = Decimal(rate)
                except InvalidOperation as exc:
                    raise _error(
                        "tax_snapshot_missing", "Frozen tax rate is invalid."
                    ) from exc
                if not frozen_rate.is_finite():
                    raise _error("tax_snapshot_missing", "Frozen tax rate is invalid.")
                tax_snapshot = InvoiceLineTaxSnapshot(
                    id=row.tax_rate_id,
                    code=code,
                    rate=frozen_rate,
                    is_active=active,
                )
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
                tax_snapshot=tax_snapshot,
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
    purchase.failure_code = None
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
    "PurchasedCoverage",
    "PurchasedCoverageQuery",
    "resolve_purchased_coverage",
    "PurchasePaymentRecoveryCommand",
    "stage_purchase_payment_recovery",
    "PurchaseRecoveryPreview",
    "PurchaseRecoveryAction",
    "PURCHASE_REPAIR_SCOPE",
    "RetryPurchaseSettlementCommand",
    "preview_purchase_recovery",
    "retry_purchase_settlement",
    "SettlePrepaidPeriodPurchaseCommand",
    "SettleVerifiedPrepaidPeriodPurchaseCommand",
    "create_prepaid_period_purchase",
    "preview_prepaid_period_purchase",
    "settle_prepaid_period_purchase",
    "settle_verified_prepaid_period_purchase",
    "stage_verified_prepaid_period_purchase",
]
