"""Reviewed construction and reconstruction of consumed prepaid service invoices.

This owner turns exact subscription, calendar, contract-price, usage and overlap
facts into one draft invoice identity.  It can also attach that identity to an
eligible existing invoice.  Issuance and payment remain with their existing
owners; the marker written here tells payment finalization to create historical
entitlement evidence without restoring current access or moving the billing
anchor.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
    TaxApplication,
    TaxRate,
)
from app.models.catalog import BillingCycle, BillingMode, CatalogOffer, Subscription
from app.models.idempotency import IdempotencyKey
from app.models.subscriber import Subscriber
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import InvoiceCreate
from app.services.audit import AuditEvents
from app.services.billing._common import lock_account
from app.services.billing.invoices import (
    ControlledInvoiceDraftSeal,
    HistoricalChargeProvenance,
    InvoiceLines,
    InvoiceLineTaxSnapshot,
    InvoiceOwnerError,
    Invoices,
    ReviewedBackdatedServiceInvoiceLineDocument,
    ReviewedPrepaidInvoiceSequenceDocument,
)
from app.services.billing_settings import resolve_payment_due_days
from app.services.common import round_money, to_decimal
from app.services.customer_financial_position import get_customer_financial_position
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.network.radius_sessions import accounting_coverage_for_period
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.prepaid_calendar_contracts import ReviewedPrepaidServicePeriodQuery
from app.services.prepaid_service_renewals import (
    PrepaidMonthlyChargeDetail,
    PrepaidServiceRenewalError,
    PrepaidSettlementPeriod,
    PrepaidSettlementPeriodQuery,
    resolve_prepaid_monthly_charge_detail,
    resolve_prepaid_settlement_period,
    resolve_reviewed_prepaid_service_period,
)

OWNER = "financial.backdated_service_invoicing"
CREATE_PERMISSION = "billing:invoice:create_backdated_service"
RECONSTRUCT_PERMISSION = "billing:invoice:reconstruct_backdated_service"
CREATE_CONCERN = "reviewed backdated subscription-service invoice construction"
RECONSTRUCT_CONCERN = "reviewed existing invoice service-period reconstruction"
MARKER_KEY = "backdated_service_invoice"
RECONSTRUCTION_REF_PREFIX = "backdated-service-invoicing:"

_CREATE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CREATE_CONCERN,
    name="create_backdated_service_invoice",
)
_RECONSTRUCT_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=RECONSTRUCT_CONCERN,
    name="reconstruct_backdated_service_invoice",
)
_CREATE_IDEMPOTENCY_SCOPE = "backdated-service-invoice-create"
_RECONSTRUCT_IDEMPOTENCY_SCOPE = "backdated-service-invoice-reconstruct"
_ELIGIBLE_RECONSTRUCTION_STATUSES = frozenset(
    {
        InvoiceStatus.draft,
        InvoiceStatus.issued,
        InvoiceStatus.partially_paid,
        InvoiceStatus.overdue,
    }
)


class BackdatedServiceInvoiceAction(StrEnum):
    create = "create"
    reconstruct = "reconstruct"


class BackdatedServiceInvoiceDisposition(StrEnum):
    actionable = "actionable"
    blocked = "blocked"


class BackdatedServiceInvoiceError(DomainError, ValueError):
    """Stable fail-closed rejection from this owner."""


def _error(
    suffix: str, message: str, **details: object
) -> BackdatedServiceInvoiceError:
    return BackdatedServiceInvoiceError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class BackdatedServiceInvoiceQuery:
    account_id: UUID
    subscription_id: UUID
    starts_on: date
    ends_on: date
    action: BackdatedServiceInvoiceAction = BackdatedServiceInvoiceAction.create
    invoice_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class BackdatedServiceInvoicePreview:
    query: BackdatedServiceInvoiceQuery
    disposition: BackdatedServiceInvoiceDisposition
    reason: str
    account_name: str
    plan_name: str
    period_start: datetime
    period_end: datetime
    currency: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    open_receivable_before: Decimal
    open_receivable_after_issue: Decimal
    prepaid_available_balance: Decimal
    usage_interval_count: int
    usage_seconds: int
    usage_source_ids: tuple[UUID, ...]
    overlapping_invoice_ids: tuple[UUID, ...]
    overlapping_entitlement_ids: tuple[UUID, ...]
    reconstruction_candidate_ids: tuple[UUID, ...]
    selected_invoice_number: str | None
    selected_invoice_status: str | None
    historical_charge_provenance: HistoricalChargeProvenance
    terms_source_invoice_ids: tuple[UUID, ...]
    fingerprint: str

    @property
    def actionable(self) -> bool:
        return self.disposition is BackdatedServiceInvoiceDisposition.actionable


@dataclass(frozen=True, slots=True)
class BackdatedServiceSubscriptionOption:
    id: UUID
    plan_name: str
    status: str
    billing_cycle: str


@dataclass(frozen=True, slots=True)
class BackdatedServiceInvoiceFormOptions:
    account_id: UUID
    account_name: str
    subscriptions: tuple[BackdatedServiceSubscriptionOption, ...]


@dataclass(frozen=True, slots=True)
class CreateBackdatedServiceInvoiceCommand:
    context: CommandContext
    query: BackdatedServiceInvoiceQuery
    expected_fingerprint: str
    permission_granted: bool


@dataclass(frozen=True, slots=True)
class BackdatedServiceInvoiceResult:
    invoice_id: UUID
    invoice_line_id: UUID
    action: BackdatedServiceInvoiceAction
    status: InvoiceStatus
    replayed: bool


@dataclass(frozen=True, slots=True)
class HistoricalChargeResolution:
    detail: PrepaidMonthlyChargeDetail
    provenance: HistoricalChargeProvenance
    source_invoice_ids: tuple[UUID, ...]
    tax_snapshot: InvoiceLineTaxSnapshot | None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _account_name(account: Subscriber) -> str:
    return (
        account.display_name
        or account.company_name
        or account.full_name
        or account.account_number
        or str(account.id)
    )


def list_form_options(
    db: Session, *, account_id: UUID
) -> BackdatedServiceInvoiceFormOptions:
    """Return typed, display-only choices for the controlled admin form."""

    account = db.get(Subscriber, account_id)
    if account is None or not account.is_active:
        raise _error("account_not_found", "Billing account was not found.")
    rows = db.execute(
        select(Subscription, CatalogOffer)
        .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
        .where(
            Subscription.subscriber_id == account_id,
            Subscription.billing_mode == BillingMode.prepaid,
        )
        .order_by(CatalogOffer.name, Subscription.id)
    ).all()
    return BackdatedServiceInvoiceFormOptions(
        account_id=account.id,
        account_name=_account_name(account),
        subscriptions=tuple(
            BackdatedServiceSubscriptionOption(
                id=subscription.id,
                plan_name=offer.name,
                status=subscription.status.value,
                billing_cycle=(subscription.billing_cycle or offer.billing_cycle).value,
            )
            for subscription, offer in rows
        ),
    )


def _resolve_subject(
    db: Session, query: BackdatedServiceInvoiceQuery
) -> tuple[Subscriber, Subscription, CatalogOffer]:
    account = db.get(Subscriber, query.account_id)
    subscription = db.get(Subscription, query.subscription_id)
    if account is None or not account.is_active:
        raise _error("account_not_found", "Billing account was not found.")
    if subscription is None or subscription.subscriber_id != account.id:
        raise _error(
            "subscription_not_found",
            "Subscription does not belong to the selected billing account.",
        )
    if subscription.billing_mode is not BillingMode.prepaid:
        raise _error(
            "subscription_not_prepaid",
            "Backdated consumed-service invoicing currently supports prepaid subscriptions only.",
        )
    offer = db.get(CatalogOffer, subscription.offer_id)
    if offer is None:
        raise _error("plan_not_found", "The subscription plan was not found.")
    later_plan_change_id = db.scalar(
        select(SubscriptionChangeRequest.id)
        .where(
            SubscriptionChangeRequest.subscription_id == subscription.id,
            SubscriptionChangeRequest.status == SubscriptionChangeStatus.applied,
            SubscriptionChangeRequest.effective_date > query.starts_on,
        )
        .order_by(
            SubscriptionChangeRequest.effective_date,
            SubscriptionChangeRequest.id,
        )
        .limit(1)
    )
    if later_plan_change_id is not None:
        raise _error(
            "historical_plan_ambiguous",
            "A later applied plan change prevents the current plan and price from proving this historical period.",
            subscription_change_id=str(later_plan_change_id),
        )
    return account, subscription, offer


def _resolve_period_and_charge(
    db: Session,
    *,
    account: Subscriber,
    subscription: Subscription,
    offer: CatalogOffer,
    query: BackdatedServiceInvoiceQuery,
) -> tuple[PrepaidSettlementPeriod, HistoricalChargeResolution]:
    try:
        period = resolve_reviewed_prepaid_service_period(
            ReviewedPrepaidServicePeriodQuery(
                starts_on=query.starts_on,
                ends_on=query.ends_on,
            )
        )
    except PrepaidServiceRenewalError as exc:
        raise _error(
            "calendar_resolution_failed",
            "The selected service dates could not be resolved.",
            source_code=exc.code,
        ) from exc
    billing_cycle = subscription.billing_cycle or offer.billing_cycle
    if billing_cycle is not BillingCycle.monthly:
        raise _error(
            "charge_unavailable",
            "Backdated consumed-service invoicing requires a monthly subscription.",
        )
    try:
        canonical = resolve_prepaid_settlement_period(
            PrepaidSettlementPeriodQuery(
                effective_at=period.starts_at,
                billing_cycle=billing_cycle,
            )
        )
    except PrepaidServiceRenewalError as exc:
        raise _error(
            "calendar_resolution_failed",
            "The contracted billing cadence could not resolve this service period.",
            source_code=exc.code,
        ) from exc
    if canonical.ends_at != period.ends_at:
        raise _error(
            "invalid_service_period",
            "The selected dates must be one exact monthly subscription period.",
            expected_ends_on=canonical.ends_on.isoformat(),
        )
    if period.ends_at > datetime.now(UTC):
        raise _error(
            "service_period_not_consumed",
            "The selected backdated service period must already be complete.",
        )
    if subscription.start_at is not None and period.starts_at < _utc(
        subscription.start_at
    ):
        raise _error(
            "period_outside_contract",
            "The service period starts before the subscription contract.",
        )
    contract_ends = tuple(
        _utc(value)
        for value in (subscription.canceled_at, subscription.end_at)
        if value is not None
    )
    contract_end = min(contract_ends) if contract_ends else None
    if contract_end is not None and period.ends_at > _utc(contract_end):
        raise _error(
            "period_outside_contract",
            "The service period ends after the subscription contract.",
        )
    return period, _resolve_historical_charge(
        db,
        account=account,
        subscription=subscription,
        offer=offer,
        period=period,
    )


def _overlapping_invoice_ids(
    db: Session,
    *,
    subscription_id: UUID,
    period: PrepaidSettlementPeriod,
    exclude_invoice_id: UUID | None,
) -> tuple[UUID, ...]:
    conditions = [
        InvoiceLine.subscription_id == subscription_id,
        InvoiceLine.is_active.is_(True),
        Invoice.is_active.is_(True),
        Invoice.status.notin_((InvoiceStatus.void, InvoiceStatus.written_off)),
        Invoice.billing_period_start < period.ends_at,
        Invoice.billing_period_end > period.starts_at,
    ]
    if exclude_invoice_id is not None:
        conditions.append(Invoice.id != exclude_invoice_id)
    return tuple(
        db.scalars(
            select(Invoice.id)
            .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
            .where(*conditions)
            .distinct()
            .order_by(Invoice.id)
        ).all()
    )


def _overlapping_entitlement_ids(
    db: Session,
    *,
    subscription_id: UUID,
    period: PrepaidSettlementPeriod,
    exclude_invoice_id: UUID | None,
) -> tuple[UUID, ...]:
    conditions = [
        ServiceEntitlement.subscription_id == subscription_id,
        ServiceEntitlement.status == ServiceEntitlementStatus.active,
        ServiceEntitlement.starts_at < period.ends_at,
        ServiceEntitlement.ends_at > period.starts_at,
    ]
    if exclude_invoice_id is not None:
        conditions.append(
            or_(
                ServiceEntitlement.source_invoice_id.is_(None),
                ServiceEntitlement.source_invoice_id != exclude_invoice_id,
            )
        )
    return tuple(
        db.scalars(
            select(ServiceEntitlement.id)
            .where(*conditions)
            .order_by(ServiceEntitlement.id)
        ).all()
    )


def _active_lines(db: Session, invoice_id: UUID) -> tuple[InvoiceLine, ...]:
    return tuple(
        db.scalars(
            select(InvoiceLine)
            .where(
                InvoiceLine.invoice_id == invoice_id,
                InvoiceLine.is_active.is_(True),
            )
            .order_by(InvoiceLine.created_at, InvoiceLine.id)
        ).all()
    )


def _reference_charge_detail(
    invoice: Invoice,
    line: InvoiceLine,
) -> tuple[PrepaidMonthlyChargeDetail, InvoiceLineTaxSnapshot | None] | None:
    line_metadata = line.metadata_ if isinstance(line.metadata_, dict) else {}
    currency = (invoice.currency or "").strip().upper()
    if (
        line.subscription_id is None
        or line_metadata.get("kind") != "base_subscription"
        or invoice.discount_type is not None
        or len(currency) != 3
        or round_money(to_decimal(line.quantity)) != Decimal("1.00")
        or round_money(to_decimal(line.amount))
        != round_money(to_decimal(line.unit_price))
        or round_money(to_decimal(line.unit_price)) <= Decimal("0.00")
    ):
        return None
    unit_price = round_money(to_decimal(line.unit_price))
    subtotal = round_money(to_decimal(invoice.subtotal))
    tax_total = round_money(to_decimal(invoice.tax_total))
    total = round_money(to_decimal(invoice.total))
    if min(subtotal, tax_total, total) < Decimal("0.00"):
        return None
    tax_snapshot: InvoiceLineTaxSnapshot | None = None
    if line.tax_rate_id is None:
        if (
            line.tax_rate_snapshot_version is not None
            or line.tax_rate_percent_snapshot is not None
            or tax_total != Decimal("0.00")
            or subtotal != unit_price
            or total != unit_price
        ):
            return None
        tax_application = TaxApplication.exempt
    else:
        if (
            line.tax_rate_snapshot_version != 1
            or line.tax_rate_percent_snapshot is None
            or line.tax_rate_is_active_snapshot is None
        ):
            return None
        tax_snapshot = InvoiceLineTaxSnapshot(
            id=line.tax_rate_id,
            code=line.tax_rate_code_snapshot,
            rate=to_decimal(line.tax_rate_percent_snapshot),
            is_active=bool(line.tax_rate_is_active_snapshot),
        )
        tax_application = line.tax_application
        if tax_application is TaxApplication.inclusive:
            if total != unit_price or round_money(subtotal + tax_total) != total:
                return None
        elif tax_application is TaxApplication.exclusive:
            if subtotal != unit_price or round_money(subtotal + tax_total) != total:
                return None
        else:
            return None
    return (
        PrepaidMonthlyChargeDetail(
            subscription_id=line.subscription_id,
            unit_price=unit_price,
            subtotal=subtotal,
            tax_total=tax_total,
            total=total,
            currency=currency,
            billing_cycle=BillingCycle.monthly,
            tax_rate_id=line.tax_rate_id,
            tax_application=tax_application,
        ),
        tax_snapshot,
    )


def _adjacent_invoice_charge_resolution(
    db: Session,
    *,
    account_id: UUID,
    subscription_id: UUID,
    period: PrepaidSettlementPeriod,
) -> HistoricalChargeResolution | None:
    invoices = tuple(
        db.scalars(
            select(Invoice)
            .where(
                Invoice.account_id == account_id,
                Invoice.is_active.is_(True),
                Invoice.is_proforma.is_not(True),
                Invoice.status.in_(
                    (
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.paid,
                        InvoiceStatus.overdue,
                    )
                ),
                or_(
                    Invoice.billing_period_end == period.starts_at,
                    Invoice.billing_period_start == period.ends_at,
                ),
            )
            .order_by(Invoice.billing_period_start, Invoice.id)
        ).all()
    )
    candidates: list[
        tuple[Invoice, PrepaidMonthlyChargeDetail, InvoiceLineTaxSnapshot | None]
    ] = []
    for invoice in invoices:
        if invoice.billing_period_start is None or invoice.billing_period_end is None:
            continue
        try:
            reference_period = resolve_prepaid_settlement_period(
                PrepaidSettlementPeriodQuery(
                    effective_at=invoice.billing_period_start,
                    billing_cycle=BillingCycle.monthly,
                )
            )
        except PrepaidServiceRenewalError:
            continue
        if reference_period.ends_at != _utc(invoice.billing_period_end):
            continue
        lines = _active_lines(db, invoice.id)
        if len(lines) != 1 or lines[0].subscription_id != subscription_id:
            continue
        resolved = _reference_charge_detail(invoice, lines[0])
        if resolved is not None:
            detail, tax_snapshot = resolved
            candidates.append((invoice, detail, tax_snapshot))
    if not candidates:
        return None
    signatures = {
        (
            detail.unit_price,
            detail.subtotal,
            detail.tax_total,
            detail.total,
            detail.currency,
            detail.tax_rate_id,
            detail.tax_application,
            tax_snapshot.code if tax_snapshot else None,
            tax_snapshot.rate if tax_snapshot else None,
        )
        for _invoice, detail, tax_snapshot in candidates
    }
    if len(signatures) != 1:
        raise _error(
            "historical_terms_ambiguous",
            "Adjacent issued invoices disagree on the historical price or tax terms.",
            invoice_ids=[str(invoice.id) for invoice, _detail, _tax in candidates],
        )
    _invoice, detail, tax_snapshot = candidates[0]
    return HistoricalChargeResolution(
        detail=detail,
        provenance=HistoricalChargeProvenance.adjacent_issued_invoice,
        source_invoice_ids=tuple(invoice.id for invoice, _detail, _tax in candidates),
        tax_snapshot=tax_snapshot,
    )


def _updated_at_or_before(value: datetime | None, boundary: datetime) -> bool:
    return value is not None and _utc(value) <= boundary


def _resolve_historical_charge(
    db: Session,
    *,
    account: Subscriber,
    subscription: Subscription,
    offer: CatalogOffer,
    period: PrepaidSettlementPeriod,
) -> HistoricalChargeResolution:
    adjacent = _adjacent_invoice_charge_resolution(
        db,
        account_id=account.id,
        subscription_id=subscription.id,
        period=period,
    )
    if adjacent is not None:
        return adjacent

    detail = resolve_prepaid_monthly_charge_detail(db, subscription, period.starts_at)
    if detail is None or detail.billing_cycle is not BillingCycle.monthly:
        raise _error(
            "charge_unavailable",
            "An exact monthly contracted price and tax treatment is required.",
        )
    tax_rate = db.get(TaxRate, detail.tax_rate_id) if detail.tax_rate_id else None
    source_timestamps = (
        subscription.updated_at,
        offer.updated_at,
        account.updated_at,
        tax_rate.updated_at if tax_rate is not None else period.starts_at,
    )
    if not all(
        _updated_at_or_before(value, period.starts_at) for value in source_timestamps
    ):
        raise _error(
            "historical_terms_unproven",
            (
                "Current subscription or tax records changed after this service "
                "period began, and no adjacent issued invoice proves the historical "
                "terms. Reconstruct the terms evidence before invoicing."
            ),
        )
    tax_snapshot = (
        InvoiceLineTaxSnapshot(
            id=tax_rate.id,
            code=tax_rate.code,
            rate=tax_rate.rate,
            is_active=tax_rate.is_active,
        )
        if tax_rate is not None
        else None
    )
    return HistoricalChargeResolution(
        detail=detail,
        provenance=HistoricalChargeProvenance.unchanged_authoritative_terms,
        source_invoice_ids=(),
        tax_snapshot=tax_snapshot,
    )


def _matches_reconstruction_amount(
    invoice: Invoice,
    lines: tuple[InvoiceLine, ...],
    detail: PrepaidMonthlyChargeDetail,
) -> bool:
    if len(lines) != 1:
        return False
    line = lines[0]
    return (
        line.subscription_id is None
        and round_money(to_decimal(line.quantity)) == Decimal("1.00")
        and round_money(to_decimal(line.unit_price)) == detail.unit_price
        and round_money(to_decimal(line.amount)) == detail.unit_price
        and line.tax_rate_id == detail.tax_rate_id
        and (
            detail.tax_rate_id is None or line.tax_application is detail.tax_application
        )
        and round_money(to_decimal(invoice.subtotal)) == detail.subtotal
        and round_money(to_decimal(invoice.tax_total)) == detail.tax_total
        and round_money(to_decimal(invoice.total)) == detail.total
        and (invoice.currency or "").upper() == detail.currency.upper()
    )


def _reconstruction_candidates(
    db: Session,
    *,
    account_id: UUID,
    detail: PrepaidMonthlyChargeDetail,
) -> tuple[UUID, ...]:
    invoices = tuple(
        db.scalars(
            select(Invoice)
            .where(
                Invoice.account_id == account_id,
                Invoice.is_active.is_(True),
                Invoice.is_proforma.is_not(True),
                Invoice.status.in_(_ELIGIBLE_RECONSTRUCTION_STATUSES),
                Invoice.billing_period_start.is_(None),
                Invoice.billing_period_end.is_(None),
            )
            .order_by(Invoice.created_at, Invoice.id)
        ).all()
    )
    return tuple(
        invoice.id
        for invoice in invoices
        if _matches_reconstruction_amount(
            invoice, _active_lines(db, invoice.id), detail
        )
    )


def _selected_reconstruction(
    db: Session,
    *,
    query: BackdatedServiceInvoiceQuery,
    detail: PrepaidMonthlyChargeDetail,
) -> tuple[Invoice, InvoiceLine]:
    if query.invoice_id is None:
        raise _error(
            "invoice_required",
            "Select the existing invoice that should receive the service identity.",
        )
    invoice = db.get(Invoice, query.invoice_id)
    if (
        invoice is None
        or invoice.account_id != query.account_id
        or not invoice.is_active
        or invoice.is_proforma
        or invoice.status not in _ELIGIBLE_RECONSTRUCTION_STATUSES
        or invoice.billing_period_start is not None
        or invoice.billing_period_end is not None
    ):
        raise _error(
            "invoice_not_reconstructable",
            "The selected invoice is not an eligible periodless customer invoice.",
        )
    lines = _active_lines(db, invoice.id)
    if not _matches_reconstruction_amount(invoice, lines, detail):
        raise _error(
            "invoice_amount_mismatch",
            "The selected invoice does not exactly match the contracted service charge and tax.",
        )
    existing_entitlement = db.scalar(
        select(ServiceEntitlement.id)
        .where(
            ServiceEntitlement.source_invoice_id == invoice.id,
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
        )
        .limit(1)
    )
    if existing_entitlement is not None:
        raise _error(
            "invoice_already_entitled",
            "The selected invoice already has active service entitlement evidence.",
        )
    return invoice, lines[0]


def _fingerprint(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def preview_backdated_service_invoice(
    db: Session, query: BackdatedServiceInvoiceQuery
) -> BackdatedServiceInvoicePreview:
    """Preview exact current facts; no authorization or mutation is performed."""

    account, subscription, offer = _resolve_subject(db, query)
    period, charge = _resolve_period_and_charge(
        db,
        account=account,
        subscription=subscription,
        offer=offer,
        query=query,
    )
    detail = charge.detail
    usage = accounting_coverage_for_period(
        db,
        subscription.id,
        period_start=period.starts_at,
        period_end=period.ends_at,
    )
    exclude_invoice_id = (
        query.invoice_id
        if query.action is BackdatedServiceInvoiceAction.reconstruct
        else None
    )
    invoice_conflicts = _overlapping_invoice_ids(
        db,
        subscription_id=subscription.id,
        period=period,
        exclude_invoice_id=exclude_invoice_id,
    )
    entitlement_conflicts = _overlapping_entitlement_ids(
        db,
        subscription_id=subscription.id,
        period=period,
        exclude_invoice_id=exclude_invoice_id,
    )
    candidates = _reconstruction_candidates(db, account_id=account.id, detail=detail)
    selected_invoice: Invoice | None = None
    selected_line: InvoiceLine | None = None
    if query.action is BackdatedServiceInvoiceAction.reconstruct:
        selected_invoice, selected_line = _selected_reconstruction(
            db, query=query, detail=detail
        )

    reasons: list[str] = []
    if not usage.complete:
        reasons.append("RADIUS evidence contains invalid intervals")
    if not usage.intervals:
        reasons.append("no exact subscription-bound usage was observed")
    if invoice_conflicts:
        reasons.append("another invoice overlaps this service period")
    if entitlement_conflicts:
        reasons.append("funded service entitlement already overlaps this period")
    if query.action is BackdatedServiceInvoiceAction.create and candidates:
        reasons.append("an existing matching invoice must be reconstructed instead")

    position = get_customer_financial_position(db, account.id)
    usage_seconds = sum(
        max(0, int((interval.ends_at - interval.starts_at).total_seconds()))
        for interval in usage.intervals
    )
    actionable = not reasons
    payload: dict[str, object] = {
        "schema_version": 1,
        "action": query.action.value,
        "account_id": str(account.id),
        "subscription_id": str(subscription.id),
        "invoice_id": str(query.invoice_id) if query.invoice_id else None,
        "period_start": period.starts_at.isoformat(),
        "period_end": period.ends_at.isoformat(),
        "plan_id": str(offer.id),
        "plan_name": offer.name,
        "subscription_status": subscription.status.value,
        "subscription_billing_mode": subscription.billing_mode.value,
        "subscription_billing_cycle": detail.billing_cycle.value,
        "subscription_start_at": (
            _utc(subscription.start_at).isoformat()
            if subscription.start_at is not None
            else None
        ),
        "subscription_end_at": (
            _utc(subscription.end_at).isoformat()
            if subscription.end_at is not None
            else None
        ),
        "subscription_canceled_at": (
            _utc(subscription.canceled_at).isoformat()
            if subscription.canceled_at is not None
            else None
        ),
        "contract_unit_price": str(detail.unit_price),
        "currency": detail.currency,
        "subtotal": str(detail.subtotal),
        "tax_total": str(detail.tax_total),
        "total": str(detail.total),
        "tax_rate_id": str(detail.tax_rate_id) if detail.tax_rate_id else None,
        "tax_application": detail.tax_application.value,
        "historical_charge_provenance": charge.provenance.value,
        "terms_source_invoice_ids": [str(value) for value in charge.source_invoice_ids],
        "tax_rate_snapshot": (
            {
                "id": str(charge.tax_snapshot.id),
                "code": charge.tax_snapshot.code,
                "rate": str(charge.tax_snapshot.rate),
                "is_active": charge.tax_snapshot.is_active,
            }
            if charge.tax_snapshot is not None
            else None
        ),
        "usage_intervals": [
            {
                "source_id": str(item.source_id),
                "starts_at": item.starts_at.isoformat(),
                "ends_at": item.ends_at.isoformat(),
            }
            for item in usage.intervals
        ],
        "usage_issues": list(usage.issues),
        "overlapping_invoice_ids": [str(value) for value in invoice_conflicts],
        "overlapping_entitlement_ids": [str(value) for value in entitlement_conflicts],
        "candidate_ids": [str(value) for value in candidates],
        "selected_invoice_status": (
            selected_invoice.status.value if selected_invoice else None
        ),
        "selected_invoice_line_id": str(selected_line.id) if selected_line else None,
        "open_receivable_before": str(position.open_invoice_balance),
        "prepaid_available_balance": str(position.prepaid_available_balance),
        "actionable": actionable,
    }
    return BackdatedServiceInvoicePreview(
        query=query,
        disposition=(
            BackdatedServiceInvoiceDisposition.actionable
            if actionable
            else BackdatedServiceInvoiceDisposition.blocked
        ),
        reason=(
            "Exact consumed-service evidence is ready for confirmation."
            if actionable
            else "; ".join(reasons).capitalize() + "."
        ),
        account_name=_account_name(account),
        plan_name=offer.name,
        period_start=period.starts_at,
        period_end=period.ends_at,
        currency=detail.currency,
        subtotal=detail.subtotal,
        tax_total=detail.tax_total,
        total=detail.total,
        open_receivable_before=position.open_invoice_balance,
        open_receivable_after_issue=(
            round_money(position.open_invoice_balance + detail.total)
            if query.action is BackdatedServiceInvoiceAction.create
            else position.open_invoice_balance
        ),
        prepaid_available_balance=position.prepaid_available_balance,
        usage_interval_count=len(usage.intervals),
        usage_seconds=usage_seconds,
        usage_source_ids=tuple(item.source_id for item in usage.intervals),
        overlapping_invoice_ids=invoice_conflicts,
        overlapping_entitlement_ids=entitlement_conflicts,
        reconstruction_candidate_ids=candidates,
        selected_invoice_number=(
            selected_invoice.invoice_number if selected_invoice else None
        ),
        selected_invoice_status=(
            selected_invoice.status.value if selected_invoice else None
        ),
        historical_charge_provenance=charge.provenance,
        terms_source_invoice_ids=charge.source_invoice_ids,
        fingerprint=_fingerprint(payload),
    )


def _authorization_permission(action: BackdatedServiceInvoiceAction) -> str:
    return (
        CREATE_PERMISSION
        if action is BackdatedServiceInvoiceAction.create
        else RECONSTRUCT_PERMISSION
    )


def _reserve_idempotency(
    db: Session,
    *,
    command: CreateBackdatedServiceInvoiceCommand,
) -> tuple[IdempotencyKey, bool]:
    if not command.context.idempotency_key:
        raise _error("idempotency_required", "An idempotency key is required.")
    scope = (
        _CREATE_IDEMPOTENCY_SCOPE
        if command.query.action is BackdatedServiceInvoiceAction.create
        else _RECONSTRUCT_IDEMPOTENCY_SCOPE
    )
    existing = db.scalar(
        select(IdempotencyKey)
        .where(
            IdempotencyKey.scope == scope,
            IdempotencyKey.key == command.context.idempotency_key,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.account_id != command.query.account_id or not existing.ref_id:
            raise _error(
                "idempotency_conflict",
                "The idempotency key was already used for a different request.",
            )
        return existing, True
    reservation = IdempotencyKey(
        scope=scope,
        key=command.context.idempotency_key,
        account_id=command.query.account_id,
    )
    db.add(reservation)
    db.flush()
    return reservation, False


def _replayed_result(
    db: Session,
    *,
    reservation: IdempotencyKey,
    command: CreateBackdatedServiceInvoiceCommand,
) -> BackdatedServiceInvoiceResult:
    invoice = db.get(Invoice, UUID(str(reservation.ref_id)))
    if invoice is None:
        raise _error(
            "idempotency_evidence_missing",
            "The prior invoice result could not be verified.",
        )
    line = db.scalar(
        select(InvoiceLine)
        .where(InvoiceLine.invoice_id == invoice.id, InvoiceLine.is_active.is_(True))
        .order_by(InvoiceLine.created_at, InvoiceLine.id)
        .limit(1)
    )
    if line is None:
        raise _error(
            "idempotency_evidence_missing",
            "The prior invoice line result could not be verified.",
        )
    metadata = line.metadata_ if isinstance(line.metadata_, dict) else {}
    created_fingerprint = str(metadata.get("preview_fingerprint") or "")
    reconstruction_ref = str(metadata.get("reviewed_prepaid_sequence_ref") or "")
    if (
        invoice.account_id != command.query.account_id
        or line.subscription_id != command.query.subscription_id
        or (
            command.query.action is BackdatedServiceInvoiceAction.create
            and created_fingerprint != command.expected_fingerprint
        )
        or (
            command.query.action is BackdatedServiceInvoiceAction.reconstruct
            and not reconstruction_ref.endswith(command.expected_fingerprint)
        )
    ):
        raise _error(
            "idempotency_conflict",
            "The idempotency key was already used for a different reviewed request.",
        )
    return BackdatedServiceInvoiceResult(
        invoice_id=invoice.id,
        invoice_line_id=line.id,
        action=command.query.action,
        status=invoice.status,
        replayed=True,
    )


def _audit_actor(context: CommandContext) -> tuple[AuditActorType, str | None]:
    if context.actor.startswith("system:"):
        return AuditActorType.system, None
    return AuditActorType.user, context.actor


def execute_backdated_service_invoice(
    db: Session,
    command: CreateBackdatedServiceInvoiceCommand,
) -> BackdatedServiceInvoiceResult:
    """Create or reconstruct one fingerprint-approved historical service invoice."""

    expected_permission = _authorization_permission(command.query.action)
    if not command.permission_granted or command.context.scope != expected_permission:
        raise _error(
            "permission_denied",
            "The required backdated service invoice permission was not granted.",
            required_permission=expected_permission,
        )
    definition = (
        _CREATE_COMMAND
        if command.query.action is BackdatedServiceInvoiceAction.create
        else _RECONSTRUCT_COMMAND
    )

    def operation() -> BackdatedServiceInvoiceResult:
        lock_account(db, str(command.query.account_id))
        locked_subscription = lock_for_update(
            db, Subscription, command.query.subscription_id
        )
        if locked_subscription is None:
            raise _error("subscription_not_found", "Subscription was not found.")
        reservation, replayed = _reserve_idempotency(db, command=command)
        if replayed:
            return _replayed_result(db, reservation=reservation, command=command)
        current = preview_backdated_service_invoice(db, command.query)
        if current.fingerprint != command.expected_fingerprint:
            raise _error(
                "stale_preview",
                "The subscription, usage, price, tax, invoice, or overlap facts changed. Review again.",
            )
        if not current.actionable:
            raise _error("not_actionable", current.reason)

        locked_account, _subject, current_offer = _resolve_subject(db, command.query)
        _period, charge = _resolve_period_and_charge(
            db,
            account=locked_account,
            subscription=locked_subscription,
            offer=current_offer,
            query=command.query,
        )
        detail = charge.detail

        description = (
            f"{current.plan_name} service "
            f"({command.query.starts_on.isoformat()} to {command.query.ends_on.isoformat()})"
        )
        if command.query.action is BackdatedServiceInvoiceAction.create:
            draft_issued_at = datetime.now(UTC)
            due_days = resolve_payment_due_days(db, subscriber=locked_account)
            try:
                invoice = Invoices.stage_system_invoice_for_owner(
                    db,
                    InvoiceCreate(
                        account_id=command.query.account_id,
                        status=InvoiceStatus.draft,
                        currency=current.currency,
                        billing_period_start=current.period_start,
                        billing_period_end=current.period_end,
                        issued_at=draft_issued_at,
                        due_at=draft_issued_at + timedelta(days=due_days),
                        due_date_basis=InvoiceDueDateBasis.contract_terms,
                        due_date_basis_ref=(
                            f"subscription:{command.query.subscription_id}"
                        ),
                        due_date_policy_version="billing-payment-terms-v1",
                        memo=(
                            "Reviewed invoice for previously consumed subscription service. "
                            "Issue only after verifying the previewed evidence."
                        ),
                    ),
                    reason=command.context.reason,
                )
                line_key_hash = hashlib.sha256(
                    (
                        f"{command.query.subscription_id}:"
                        f"{current.period_start.isoformat()}:"
                        f"{current.period_end.isoformat()}"
                    ).encode()
                ).hexdigest()
                line = InvoiceLines.stage_reviewed_backdated_service_line_for_owner(
                    db,
                    ReviewedBackdatedServiceInvoiceLineDocument(
                        invoice_id=invoice.id,
                        subscription_id=command.query.subscription_id,
                        description=description,
                        unit_price=detail.unit_price,
                        tax_rate_id=detail.tax_rate_id,
                        tax_application=detail.tax_application,
                        tax_snapshot=charge.tax_snapshot,
                        billing_period_start=current.period_start,
                        billing_period_end=current.period_end,
                        preview_fingerprint=current.fingerprint,
                        historical_charge_provenance=charge.provenance,
                        terms_source_invoice_ids=charge.source_invoice_ids,
                        billing_line_key=f"backdated-service:{line_key_hash}",
                    ),
                    reason=command.context.reason,
                )
                invoice = Invoices.recalculate_totals_for_owner(db, invoice.id)
            except InvoiceOwnerError as exc:
                raise _error(
                    "invoice_participant_rejected",
                    "Invoice construction was rejected by the invoice owner.",
                    source_code=exc.code,
                ) from exc
            if (
                round_money(to_decimal(invoice.subtotal)) != current.subtotal
                or round_money(to_decimal(invoice.tax_total)) != current.tax_total
                or round_money(to_decimal(invoice.total)) != current.total
            ):
                raise _error(
                    "constructed_total_mismatch",
                    "The staged invoice no longer matches the reviewed charge.",
                )
            try:
                invoice = Invoices.seal_controlled_draft_for_owner(
                    db,
                    invoice.id,
                    ControlledInvoiceDraftSeal(
                        controller_owner=OWNER,
                        preview_fingerprint=current.fingerprint,
                    ),
                )
            except InvoiceOwnerError as exc:
                raise _error(
                    "invoice_participant_rejected",
                    "Invoice draft sealing was rejected by the invoice owner.",
                    source_code=exc.code,
                ) from exc
            event_type = EventType.backdated_service_invoice_created
            audit_action = "create_backdated_service_invoice"
        else:
            selected_invoice, selected_line = _selected_reconstruction(
                db,
                query=command.query,
                detail=detail,
            )
            evidence_ref = (
                f"{RECONSTRUCTION_REF_PREFIX}{command.context.command_id}:"
                f"{current.fingerprint}"
            )
            try:
                invoice = Invoices.adopt_reviewed_prepaid_sequence_document_for_owner(
                    db,
                    ReviewedPrepaidInvoiceSequenceDocument(
                        invoice_id=selected_invoice.id,
                        line_id=selected_line.id,
                        subscription_id=command.query.subscription_id,
                        billing_period_start=current.period_start,
                        billing_period_end=current.period_end,
                        expected_status=selected_invoice.status,
                        expected_line_quantity=to_decimal(selected_line.quantity),
                        expected_line_unit_price=to_decimal(selected_line.unit_price),
                        expected_line_amount=to_decimal(selected_line.amount),
                        line_description=description,
                        evidence_ref=evidence_ref,
                    ),
                )
            except InvoiceOwnerError as exc:
                raise _error(
                    "invoice_participant_rejected",
                    "Invoice reconstruction was rejected by the invoice owner.",
                    source_code=exc.code,
                ) from exc
            line = selected_line
            if invoice.status is InvoiceStatus.draft:
                try:
                    invoice = Invoices.seal_controlled_draft_for_owner(
                        db,
                        invoice.id,
                        ControlledInvoiceDraftSeal(
                            controller_owner=OWNER,
                            preview_fingerprint=current.fingerprint,
                        ),
                    )
                except InvoiceOwnerError as exc:
                    raise _error(
                        "invoice_participant_rejected",
                        "Reconstructed draft sealing was rejected by the invoice owner.",
                        source_code=exc.code,
                    ) from exc
            event_type = EventType.backdated_service_invoice_reconstructed
            audit_action = "reconstruct_backdated_service_invoice"

        reservation.ref_id = str(invoice.id)
        actor_type, actor_id = _audit_actor(command.context)
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_type=actor_type,
                actor_id=actor_id,
                action=audit_action,
                entity_type="invoice",
                entity_id=str(invoice.id),
                request_id=str(command.context.correlation_id),
                metadata_={
                    "owner": OWNER,
                    "account_id": str(command.query.account_id),
                    "subscription_id": str(command.query.subscription_id),
                    "invoice_id": str(invoice.id),
                    "invoice_line_id": str(line.id),
                    "period_start": current.period_start.isoformat(),
                    "period_end": current.period_end.isoformat(),
                    "total": str(current.total),
                    "currency": current.currency,
                    "usage_interval_count": current.usage_interval_count,
                    "historical_charge_provenance": (
                        current.historical_charge_provenance.value
                    ),
                    "terms_source_invoice_ids": [
                        str(value) for value in current.terms_source_invoice_ids
                    ],
                    "preview_fingerprint": current.fingerprint,
                    "scope": command.context.scope,
                    "reason": command.context.reason,
                },
            ),
        )
        emit_event(
            db,
            event_type,
            {
                "schema_version": 1,
                "account_id": str(command.query.account_id),
                "subscription_id": str(command.query.subscription_id),
                "invoice_id": str(invoice.id),
                "invoice_line_id": str(line.id),
                "period_start": current.period_start.isoformat(),
                "period_end": current.period_end.isoformat(),
                "total": str(current.total),
                "currency": current.currency,
                "historical_charge_provenance": (
                    current.historical_charge_provenance.value
                ),
                "terms_source_invoice_ids": [
                    str(value) for value in current.terms_source_invoice_ids
                ],
                "preview_fingerprint": current.fingerprint,
            },
            actor=command.context.actor,
            account_id=command.query.account_id,
            subscription_id=command.query.subscription_id,
            invoice_id=invoice.id,
        )
        db.flush()
        return BackdatedServiceInvoiceResult(
            invoice_id=invoice.id,
            invoice_line_id=line.id,
            action=command.query.action,
            status=invoice.status,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=definition,
        context=command.context,
        operation=operation,
    )
