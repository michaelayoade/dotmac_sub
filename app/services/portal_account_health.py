"""Shared Customer and Reseller portal Account/Service Health projection.

This read owner composes existing lifecycle, financial, access, live-session,
connection-diagnosis, and outage owners. It does not poll equipment, decide a
transition, or mutate state. Customer and reseller templates render the same
facts while adapting navigation to their audience.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from math import ceil
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.models.usage import QuotaBucket
from app.schemas.service_status import ServiceStatusAction, ServiceStatusItem
from app.schemas.status_presentation import StatusPresentation
from app.services import display_format
from app.services.billing_profile import resolve_billing_profile
from app.services.common import coerce_uuid
from app.services.customer_financial_position import (
    get_customer_receivable_summaries,
    prepaid_available_balance,
)
from app.services.domain_errors import DomainError
from app.services.network.radius_sessions import (
    SubscriptionSessionSnapshot,
    subscription_session_snapshots,
)
from app.services.prepaid_currency import resolve_prepaid_enforcement_currency
from app.services.prepaid_funding_reconstruction import (
    PrepaidFundingBaselineMissingError,
)
from app.services.service_status import (
    OperationalSubscriptionCohort,
    build_service_status,
    resolve_operational_subscription_cohort,
)
from app.services.status_presentation import (
    access_session_status_presentation,
    account_status_presentation,
    invoice_status_presentation,
    service_access_status_presentation,
    subscription_status_presentation,
    ticket_status_presentation,
)
from app.services.subscription_lifecycle import classify_service_change_delivery
from app.services.topology.connection_status import assess
from app.services.ui_contracts import StateValue

logger = logging.getLogger(__name__)


class PortalServiceAccessState(StrEnum):
    available = "available"
    restricted = "restricted"
    unavailable = "unavailable"


@dataclass(frozen=True, slots=True)
class MoneyAmount:
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class ReceivableLane:
    currency: str
    outstanding: Decimal
    overdue: Decimal
    overdue_count: int


@dataclass(frozen=True, slots=True)
class PortalFinancialHealth:
    billing_mode: StateValue
    billing_mode_reason: str
    receivables: StateValue
    prepaid_funding: StateValue
    prepaid_funding_reason: str


@dataclass(frozen=True, slots=True)
class PortalConnectionDiagnosis:
    state: str
    status_presentation: StatusPresentation
    headline: str
    message: str
    advice: str | None
    medium: str | None
    area_outage: bool
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class PortalRemoteProvisionPriceReview:
    fingerprint: str
    effective_at: datetime
    previous_amount: Decimal
    current_amount: Decimal
    currency: str
    available_balance: Decimal
    shortfall: Decimal
    allowed: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class PortalPendingServiceChange:
    request_id: UUID
    status: str
    target_offer_name: str
    effective_date: date
    delivery_mode: str
    delivery_state: str
    target_service_address: str | None
    field_fee_amount: Decimal | None
    field_fee_currency: str | None
    price_review: PortalRemoteProvisionPriceReview | None


@dataclass(frozen=True, slots=True)
class PortalServiceHealth:
    subscription_id: UUID
    offer_name: str
    lifecycle: StatusPresentation
    billing_mode: str | None
    access_state: PortalServiceAccessState
    access: StatusPresentation
    access_reason: str
    session: SubscriptionSessionSnapshot
    session_presentation: StatusPresentation
    connection: StateValue
    next_charge_at: datetime | None
    expires_at: datetime | None
    next_action: ServiceStatusAction | None
    customer_action_url: str | None
    pending_change: PortalPendingServiceChange | None


@dataclass(frozen=True, slots=True)
class PortalAccountHealth:
    account_id: UUID
    account_number: str | None
    subscriber_number: str | None
    display_name: str
    lifecycle: StatusPresentation
    financial: PortalFinancialHealth
    services: tuple[PortalServiceHealth, ...]
    primary_action: ServiceStatusAction | None
    customer_primary_action_url: str | None
    as_of: datetime

    @property
    def has_partial_data(self) -> bool:
        if not self.financial.billing_mode.is_present:
            return True
        if not self.financial.receivables.is_present:
            return True
        if self.financial.prepaid_funding.kind.value in {"unknown", "unavailable"}:
            return True
        return any(
            service.access_state == PortalServiceAccessState.unavailable
            or service.connection.kind.value == "unavailable"
            for service in self.services
        )

    def for_subscription(self, subscription_id: UUID) -> PortalAccountHealth:
        service = next(
            (item for item in self.services if item.subscription_id == subscription_id),
            None,
        )
        if service is None:
            raise ValueError(
                f"Subscription {subscription_id} is not in this account projection"
            )
        return replace(
            self,
            services=(service,),
            primary_action=service.next_action,
            customer_primary_action_url=service.customer_action_url,
        )

    def for_active_services(self) -> PortalAccountHealth:
        """Narrow the projection to services in the active lifecycle state."""
        active_services = tuple(
            service
            for service in self.services
            if service.lifecycle.value == SubscriptionStatus.active.value
        )
        return replace(
            self,
            services=active_services,
            primary_action=None,
            customer_primary_action_url=None,
        )


@dataclass(frozen=True, slots=True)
class CustomerDashboardHighlightsQuery:
    """Compose dashboard-only facts around an already scoped account projection."""

    account_health: PortalAccountHealth


@dataclass(frozen=True, slots=True)
class CustomerDashboardService:
    health: PortalServiceHealth
    download_mbps: int | None
    upload_mbps: int | None
    service_period: StateValue
    data_usage: StateValue


@dataclass(frozen=True, slots=True)
class CustomerDashboardPeriod:
    starts_at: datetime
    ends_at: datetime
    elapsed_percent: int
    elapsed_days: int
    total_days: int
    days_remaining: int


@dataclass(frozen=True, slots=True)
class CustomerDashboardUsage:
    used_gb: Decimal
    allowance_gb: Decimal
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class CustomerDashboardSupport:
    open_count: int
    latest_ticket_number: str | None
    latest_status_label: str | None
    latest_updated_at: datetime | None


class CustomerDashboardActivityKind(StrEnum):
    invoice = "invoice"
    connection = "connection"


@dataclass(frozen=True, slots=True)
class CustomerDashboardActivity:
    kind: CustomerDashboardActivityKind
    title: str
    detail: str
    observed_at: datetime
    action_label: str
    action_url: str
    amount_due: MoneyAmount | None = None
    due_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CustomerDashboardHighlights:
    greeting_name: str
    greeting_period: str
    observed_at: datetime
    account_credit: StateValue
    services: tuple[CustomerDashboardService, ...]
    support: StateValue
    activity: StateValue
    activity_partial: bool


_CUSTOMER_ACTION_URLS = {
    "top_up": "/portal/billing/topup",
    "pay_invoices": "/portal/billing",
    "view_usage": "/portal/usage",
    "contact_support": "/portal/support/new",
}


def _display_name(account: Subscriber) -> str:
    return (
        account.company_name
        or account.display_name
        or " ".join(
            part for part in (account.first_name, account.last_name) if part
        ).strip()
        or account.email
        or account.account_number
        or "Customer"
    )


def _customer_action_url(action: ServiceStatusAction | None) -> str | None:
    if action is None:
        return None
    return _CUSTOMER_ACTION_URLS.get(action.kind.value)


def _financial_health(db: Session, account: Subscriber) -> PortalFinancialHealth:
    profile = resolve_billing_profile(db, account)
    if profile.effective_mode is None:
        billing_mode = StateValue.unknown()
    else:
        billing_mode = StateValue.present(
            profile.effective_mode.value.replace("_", " ").title()
        )
    billing_mode_reason = (
        profile.invalid_reason.value.replace("_", " ").capitalize()
        if profile.invalid_reason
        else f"Resolved from {profile.source.value.replace('_', ' ')}."
    )

    try:
        summaries = get_customer_receivable_summaries(
            db,
            account.id,
            default_currency=display_format.default_currency(db),
        )
        receivables = StateValue.present(
            tuple(
                ReceivableLane(
                    currency=summary.currency,
                    outstanding=summary.outstanding,
                    overdue=summary.overdue,
                    overdue_count=summary.overdue_count,
                )
                for summary in summaries
            )
        )
    except Exception as exc:
        logger.warning(
            "portal_receivables_unavailable",
            extra={"account_id": str(account.id), "error_type": type(exc).__name__},
            exc_info=True,
        )
        receivables = StateValue.unavailable()

    funding_reason = "Not applicable to a postpaid account."
    if profile.effective_mode == BillingMode.prepaid:
        try:
            currency = resolve_prepaid_enforcement_currency(db)
            funding = StateValue.present(
                MoneyAmount(
                    amount=prepaid_available_balance(
                        db,
                        account.id,
                        currency=currency,
                    ),
                    currency=currency,
                )
            )
            funding_reason = (
                "Reviewed opening position plus canonical native events in the "
                "enforcement currency."
            )
        except (DomainError, PrepaidFundingBaselineMissingError, ValueError) as exc:
            logger.warning(
                "portal_prepaid_funding_unavailable",
                extra={"account_id": str(account.id), "error_type": type(exc).__name__},
            )
            funding = StateValue.unavailable()
            funding_reason = "Authoritative prepaid funding evidence is unavailable."
    elif profile.effective_mode == BillingMode.postpaid:
        funding = StateValue.not_applicable()
    else:
        funding = StateValue.unknown()
        funding_reason = "Billing mode is unresolved; funding cannot be classified."

    return PortalFinancialHealth(
        billing_mode=billing_mode,
        billing_mode_reason=billing_mode_reason,
        receivables=receivables,
        prepaid_funding=funding,
        prepaid_funding_reason=funding_reason,
    )


def _status_items(
    db: Session,
    account_id: UUID,
    *,
    as_of: datetime,
    cohort: OperationalSubscriptionCohort,
) -> tuple[dict[UUID, ServiceStatusItem], ServiceStatusAction | None, datetime]:
    try:
        response = build_service_status(
            db,
            account_id,
            as_of=as_of,
            cohort=cohort,
        )
    except (DomainError, PrepaidFundingBaselineMissingError, ValueError) as exc:
        logger.warning(
            "portal_service_status_unavailable",
            extra={"account_id": str(account_id), "error_type": type(exc).__name__},
        )
        return {}, None, as_of
    return (
        {item.subscription_id: item for item in response.services},
        response.primary_action,
        response.as_of,
    )


def _connection_diagnosis(db: Session, subscription) -> StateValue:
    if subscription.status != SubscriptionStatus.active:
        return StateValue.not_applicable()
    try:
        result = assess(db, subscription)
    except Exception:
        logger.warning(
            "portal_connection_diagnosis_unavailable",
            extra={"subscription_id": str(subscription.id)},
            exc_info=True,
        )
        return StateValue.unavailable()
    return StateValue.present(
        PortalConnectionDiagnosis(
            state=result.state,
            status_presentation=result.status_presentation,
            headline=result.headline,
            message=result.message,
            advice=result.advice,
            medium=result.medium,
            area_outage=result.is_area_outage,
            checked_at=result.checked_at,
        ),
        as_of=result.checked_at,
    )


def _service_health(
    db: Session,
    subscription,
    status_item: ServiceStatusItem | None,
    session: SubscriptionSessionSnapshot,
    pending_change: PortalPendingServiceChange | None,
) -> PortalServiceHealth:
    if status_item is None:
        access_state = PortalServiceAccessState.unavailable
        access_reason = "Service access could not be resolved."
        billing_mode = None
        action = None
        next_charge_at = subscription.next_billing_at
        expires_at = None
    else:
        access_state = (
            PortalServiceAccessState.available
            if status_item.usable
            else PortalServiceAccessState.restricted
        )
        access_reason = (
            status_item.action.message
            if status_item.action is not None
            else "No access hold is active."
        )
        billing_mode = status_item.billing_mode
        action = status_item.action
        next_charge_at = status_item.next_charge_at
        expires_at = status_item.expires_at

    return PortalServiceHealth(
        subscription_id=subscription.id,
        offer_name=(subscription.offer.name if subscription.offer else "Service"),
        lifecycle=subscription_status_presentation(subscription.status),
        billing_mode=billing_mode,
        access_state=access_state,
        access=service_access_status_presentation(access_state.value),
        access_reason=access_reason,
        session=session,
        session_presentation=access_session_status_presentation(session.state.value),
        connection=_connection_diagnosis(db, subscription),
        next_charge_at=next_charge_at,
        expires_at=expires_at,
        next_action=action,
        customer_action_url=_customer_action_url(action),
        pending_change=pending_change,
    )


def _review_decimal(raw: dict[object, object], key: str) -> Decimal | None:
    try:
        return Decimal(str(raw[key])).quantize(Decimal("0.01"))
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return None


def _remote_price_review(
    confirmation: dict[object, object],
) -> PortalRemoteProvisionPriceReview | None:
    raw = confirmation.get("provisioning_price_review")
    if not isinstance(raw, dict):
        return None
    fingerprint = str(raw.get("fingerprint") or "").strip()
    effective_raw = str(raw.get("effective_at") or "").strip()
    previous_amount = _review_decimal(raw, "previous_amount")
    current_amount = _review_decimal(raw, "current_amount")
    available_balance = _review_decimal(raw, "available_balance")
    shortfall = _review_decimal(raw, "shortfall")
    if (
        not fingerprint
        or not effective_raw
        or previous_amount is None
        or current_amount is None
        or available_balance is None
        or shortfall is None
    ):
        return None
    try:
        effective_at = datetime.fromisoformat(effective_raw)
    except ValueError:
        return None
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=UTC)
    else:
        effective_at = effective_at.astimezone(UTC)
    return PortalRemoteProvisionPriceReview(
        fingerprint=fingerprint,
        effective_at=effective_at,
        previous_amount=previous_amount,
        current_amount=current_amount,
        currency=str(raw.get("currency") or "NGN").upper(),
        available_balance=available_balance,
        shortfall=shortfall,
        allowed=bool(raw.get("allowed", False)),
        reason=str(raw.get("reason") or "").strip() or None,
    )


def _pending_service_changes(
    db: Session,
    subscriptions: list[Subscription],
) -> dict[UUID, PortalPendingServiceChange]:
    subscription_ids = [subscription.id for subscription in subscriptions]
    if not subscription_ids:
        return {}
    rows = db.scalars(
        select(SubscriptionChangeRequest)
        .where(SubscriptionChangeRequest.subscription_id.in_(subscription_ids))
        .where(
            SubscriptionChangeRequest.status.in_(
                {
                    SubscriptionChangeStatus.pending,
                    SubscriptionChangeStatus.approved,
                }
            )
        )
        .where(SubscriptionChangeRequest.applied_at.is_(None))
        .where(SubscriptionChangeRequest.is_active.is_(True))
        .options(
            selectinload(SubscriptionChangeRequest.current_offer),
            selectinload(SubscriptionChangeRequest.requested_offer),
            selectinload(SubscriptionChangeRequest.target_service_address),
        )
        .order_by(
            SubscriptionChangeRequest.effective_date.asc(),
            SubscriptionChangeRequest.created_at.asc(),
        )
    ).all()
    changes: dict[UUID, PortalPendingServiceChange] = {}
    for row in rows:
        if row.subscription_id in changes:
            continue
        delivery_mode = (
            classify_service_change_delivery(
                row.current_offer,
                row.requested_offer,
                service_address_changed=row.target_service_address_id is not None,
            ).value
            if row.current_offer is not None and row.requested_offer is not None
            else "unknown"
        )
        confirmation = row.confirmation_snapshot or {}
        target_address = row.target_service_address
        changes[row.subscription_id] = PortalPendingServiceChange(
            request_id=row.id,
            status=row.status.value,
            target_offer_name=(
                row.requested_offer.name if row.requested_offer else "Requested plan"
            ),
            effective_date=row.effective_date,
            delivery_mode=delivery_mode,
            delivery_state=str(
                confirmation.get("delivery_state") or "awaiting_verification"
            ),
            target_service_address=(
                ", ".join(
                    part
                    for part in (
                        target_address.label,
                        target_address.address_line1,
                        target_address.city,
                        target_address.region,
                    )
                    if part
                )
                if target_address is not None
                else None
            ),
            field_fee_amount=row.field_fee_amount,
            field_fee_currency=row.field_fee_currency,
            price_review=_remote_price_review(confirmation),
        )
    return changes


def build_portal_account_health(
    db: Session,
    account_id: UUID,
    *,
    as_of: datetime | None = None,
) -> PortalAccountHealth:
    """Build the shared first-viewport projection for one exact account."""
    evaluated_at = as_of or datetime.now(UTC)
    resolved_account_id = coerce_uuid(account_id)
    account = db.get(Subscriber, resolved_account_id)
    if account is None:
        raise ValueError(f"Subscriber {resolved_account_id} was not found")

    cohort = resolve_operational_subscription_cohort(
        db,
        subscriber_id=resolved_account_id,
        as_of=evaluated_at,
    )
    subscriptions = list(cohort.subscriptions)
    sessions = subscription_session_snapshots(db, subscriptions)
    pending_changes = _pending_service_changes(db, subscriptions)
    status_items, primary_action, evaluated_at = _status_items(
        db,
        resolved_account_id,
        as_of=evaluated_at,
        cohort=cohort,
    )
    service_rows = tuple(
        _service_health(
            db,
            subscription,
            status_items.get(subscription.id),
            sessions[subscription.id],
            pending_changes.get(subscription.id),
        )
        for subscription in subscriptions
    )
    return PortalAccountHealth(
        account_id=resolved_account_id,
        account_number=account.account_number,
        subscriber_number=account.subscriber_number,
        display_name=_display_name(account),
        lifecycle=account_status_presentation(
            account.status,
            is_active=account.is_active,
        ),
        financial=_financial_health(db, account),
        services=service_rows,
        primary_action=primary_action,
        customer_primary_action_url=_customer_action_url(primary_action),
        as_of=evaluated_at,
    )


def _dashboard_support(db: Session, account_id: UUID) -> StateValue:
    """Project the exact account's active, non-terminal support requests."""
    try:
        from app.services.support import Tickets, TicketStatusScope

        status_scope = TicketStatusScope.not_closed()
        open_count = Tickets.count(
            db,
            subscriber_id=str(account_id),
            is_active=True,
            status_scope=status_scope,
        )
        latest_rows = Tickets.list(
            db,
            subscriber_id=str(account_id),
            is_active=True,
            status_scope=status_scope,
            order_by="updated_at",
            order_dir="desc",
            limit=1,
        )
        latest = latest_rows[0] if latest_rows else None
        return StateValue.present(
            CustomerDashboardSupport(
                open_count=open_count,
                latest_ticket_number=latest.number if latest is not None else None,
                latest_status_label=(
                    ticket_status_presentation(latest.status).label
                    if latest is not None
                    else None
                ),
                latest_updated_at=latest.updated_at if latest is not None else None,
            ),
            as_of=datetime.now(UTC),
        )
    except Exception as exc:
        logger.warning(
            "customer_dashboard_support_unavailable",
            extra={"account_id": str(account_id), "error_type": type(exc).__name__},
        )
        return StateValue.unavailable()


def _dashboard_activity(
    db: Session, health: PortalAccountHealth
) -> tuple[StateValue, bool]:
    """Compose customer-safe invoice and connection updates from their owners."""
    items: list[CustomerDashboardActivity] = []
    partial = any(
        service.connection.kind.value in {"unknown", "unavailable"}
        for service in health.services
    )
    try:
        invoice = db.scalar(
            select(Invoice)
            .where(Invoice.account_id == health.account_id)
            .where(Invoice.is_active.is_(True))
            .where(Invoice.is_proforma.is_not(True))
            .where(
                Invoice.status.in_(
                    (
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    )
                )
            )
            .where(Invoice.balance_due > Decimal("0"))
            .order_by(Invoice.issued_at.desc(), Invoice.created_at.desc())
            .limit(1)
        )
        if invoice is not None:
            number = invoice.invoice_number or str(invoice.id)[:8]
            title = (
                f"Invoice {number} is ready"
                if invoice.status == InvoiceStatus.issued
                else f"Invoice {number} · {invoice_status_presentation(invoice.status).label}"
            )
            observed_at = invoice.issued_at or invoice.created_at
            items.append(
                CustomerDashboardActivity(
                    kind=CustomerDashboardActivityKind.invoice,
                    title=title,
                    detail="Review your latest open invoice.",
                    observed_at=(
                        observed_at.replace(tzinfo=UTC)
                        if observed_at.tzinfo is None
                        else observed_at
                    ),
                    action_label="Review",
                    action_url=f"/portal/billing/invoices/{invoice.id}",
                    amount_due=MoneyAmount(
                        amount=invoice.balance_due, currency=invoice.currency
                    ),
                    due_at=(
                        invoice.due_at
                        if invoice.due_date_basis
                        not in (None, InvoiceDueDateBasis.unknown_unverified)
                        else None
                    ),
                )
            )
    except Exception as exc:
        logger.warning(
            "customer_dashboard_invoice_activity_unavailable",
            extra={
                "account_id": str(health.account_id),
                "error_type": type(exc).__name__,
            },
        )
        partial = True

    connected_services = [
        service for service in health.services if service.connection.is_present
    ]
    if connected_services:
        service = max(
            connected_services,
            key=lambda candidate: candidate.connection.value.checked_at,
        )
        diagnosis = service.connection.value
        items.append(
            CustomerDashboardActivity(
                kind=CustomerDashboardActivityKind.connection,
                title=(
                    "Connection status may be out of date"
                    if service.connection.is_stale
                    else diagnosis.headline
                ),
                detail=diagnosis.message,
                observed_at=diagnosis.checked_at,
                action_label="Connection details",
                action_url=f"/portal/services/{service.subscription_id}",
            )
        )
    if partial and not items:
        return StateValue.unavailable(), True
    return StateValue.present(tuple(items), as_of=datetime.now(UTC)), partial


def get_customer_dashboard_highlights(
    db: Session,
    query: CustomerDashboardHighlightsQuery,
) -> CustomerDashboardHighlights:
    """Read scoped dashboard facts without introducing a second decision owner."""
    health = query.account_health
    account = db.get(Subscriber, health.account_id)
    greeting_name = (
        (account.first_name or account.company_name or health.display_name)
        if account is not None
        else health.display_name
    )
    local_hour = health.as_of.astimezone(ZoneInfo("Africa/Lagos")).hour
    greeting_period = (
        "morning" if local_hour < 12 else "afternoon" if local_hour < 17 else "evening"
    )

    credit = StateValue.unavailable()
    try:
        lanes = (
            health.financial.receivables.value
            if health.financial.receivables.is_present
            else ()
        )
        if len(lanes) <= 1:
            from app.services.billing._common import get_account_credit_balance

            currency = (
                lanes[0].currency if lanes else display_format.default_currency(db)
            )
            if health.financial.prepaid_funding.is_present:
                currency = health.financial.prepaid_funding.value.currency
            amount = get_account_credit_balance(
                db, str(health.account_id), currency=currency
            )
            if amount >= Decimal("0.00"):
                credit = StateValue.present(
                    MoneyAmount(amount=amount, currency=currency),
                    as_of=health.as_of,
                )
    except Exception as exc:
        logger.warning(
            "customer_dashboard_credit_unavailable",
            extra={
                "account_id": str(health.account_id),
                "error_type": type(exc).__name__,
            },
        )

    subscription_ids = tuple(service.subscription_id for service in health.services)
    subscriptions: dict[UUID, Subscription] = {}
    if subscription_ids:
        try:
            subscriptions = {
                row.id: row
                for row in db.scalars(
                    select(Subscription)
                    .where(Subscription.id.in_(subscription_ids))
                    .where(Subscription.subscriber_id == health.account_id)
                    .options(selectinload(Subscription.offer))
                ).all()
            }
        except Exception as exc:
            logger.warning(
                "customer_dashboard_plan_speed_unavailable",
                extra={
                    "account_id": str(health.account_id),
                    "error_type": type(exc).__name__,
                },
            )
    period_by_service: dict[UUID, StateValue] = {}
    usage_by_service: dict[UUID, StateValue] = {}
    if subscription_ids:
        try:
            entitlement_rows = db.scalars(
                select(ServiceEntitlement)
                .where(ServiceEntitlement.account_id == health.account_id)
                .where(ServiceEntitlement.subscription_id.in_(subscription_ids))
                .where(ServiceEntitlement.status == ServiceEntitlementStatus.active)
                .where(ServiceEntitlement.starts_at <= health.as_of)
                .where(ServiceEntitlement.ends_at > health.as_of)
            ).all()
            entitlements: dict[UUID, list[ServiceEntitlement]] = {}
            for entitlement_row in entitlement_rows:
                entitlements.setdefault(entitlement_row.subscription_id, []).append(
                    entitlement_row
                )
            for service_id in subscription_ids:
                entitlement_candidates = entitlements.get(service_id, [])
                if len(entitlement_candidates) != 1:
                    period_by_service[service_id] = StateValue.unknown()
                    continue
                entitlement_row = entitlement_candidates[0]
                starts_at = (
                    entitlement_row.starts_at.replace(tzinfo=UTC)
                    if entitlement_row.starts_at.tzinfo is None
                    else entitlement_row.starts_at
                )
                ends_at = (
                    entitlement_row.ends_at.replace(tzinfo=UTC)
                    if entitlement_row.ends_at.tzinfo is None
                    else entitlement_row.ends_at
                )
                duration = (ends_at - starts_at).total_seconds()
                if duration <= 0:
                    period_by_service[service_id] = StateValue.unknown()
                    continue
                remaining = (ends_at - health.as_of).total_seconds()
                total_days = ceil(duration / 86400)
                period_by_service[service_id] = StateValue.present(
                    CustomerDashboardPeriod(
                        starts_at=starts_at,
                        ends_at=ends_at,
                        elapsed_percent=max(
                            0, min(100, round((1 - remaining / duration) * 100))
                        ),
                        elapsed_days=max(
                            1,
                            min(total_days, ceil((duration - remaining) / 86400)),
                        ),
                        total_days=total_days,
                        days_remaining=max(0, ceil(remaining / 86400)),
                    ),
                    as_of=health.as_of,
                )
        except Exception as exc:
            logger.warning(
                "customer_dashboard_period_unavailable",
                extra={
                    "account_id": str(health.account_id),
                    "error_type": type(exc).__name__,
                },
            )
            period_by_service = {
                service_id: StateValue.unavailable() for service_id in subscription_ids
            }

        try:
            bucket_rows = db.scalars(
                select(QuotaBucket)
                .where(QuotaBucket.subscription_id.in_(subscription_ids))
                .where(QuotaBucket.period_start <= health.as_of)
                .where(QuotaBucket.period_end > health.as_of)
            ).all()
            buckets: dict[UUID, list[QuotaBucket]] = {}
            for bucket_row in bucket_rows:
                buckets.setdefault(bucket_row.subscription_id, []).append(bucket_row)
            for service_id in subscription_ids:
                subscription = subscriptions.get(service_id)
                allowance_id = (
                    subscription.offer.usage_allowance_id
                    if subscription is not None and subscription.offer is not None
                    else None
                )
                bucket_candidates = [
                    candidate
                    for candidate in buckets.get(service_id, [])
                    if allowance_id is not None
                    and candidate.usage_allowance_id == allowance_id
                ]
                if len(bucket_candidates) != 1:
                    usage_by_service[service_id] = StateValue.unknown()
                    continue
                bucket_row = bucket_candidates[0]
                if bucket_row.included_gb is None:
                    usage_by_service[service_id] = StateValue.not_applicable()
                    continue
                allowance_gb = (
                    bucket_row.included_gb
                    + (bucket_row.rollover_gb or Decimal("0"))
                    + (bucket_row.topup_gb or Decimal("0"))
                )
                if (
                    bucket_row.used_gb is None
                    or bucket_row.used_gb < 0
                    or allowance_gb < 0
                ):
                    usage_by_service[service_id] = StateValue.unknown()
                    continue
                usage_by_service[service_id] = StateValue.present(
                    CustomerDashboardUsage(
                        used_gb=bucket_row.used_gb,
                        allowance_gb=allowance_gb,
                        starts_at=bucket_row.period_start,
                        ends_at=bucket_row.period_end,
                    ),
                    as_of=bucket_row.updated_at.replace(tzinfo=UTC)
                    if bucket_row.updated_at.tzinfo is None
                    else bucket_row.updated_at,
                )
        except Exception as exc:
            logger.warning(
                "customer_dashboard_usage_unavailable",
                extra={
                    "account_id": str(health.account_id),
                    "error_type": type(exc).__name__,
                },
            )
            usage_by_service = {
                service_id: StateValue.unavailable() for service_id in subscription_ids
            }

    services = tuple(
        CustomerDashboardService(
            health=service,
            download_mbps=(
                subscriptions[service.subscription_id].offer.speed_download_mbps
                if service.subscription_id in subscriptions
                and subscriptions[service.subscription_id].offer is not None
                else None
            ),
            upload_mbps=(
                subscriptions[service.subscription_id].offer.speed_upload_mbps
                if service.subscription_id in subscriptions
                and subscriptions[service.subscription_id].offer is not None
                else None
            ),
            service_period=period_by_service.get(
                service.subscription_id, StateValue.unknown()
            ),
            data_usage=usage_by_service.get(
                service.subscription_id, StateValue.unknown()
            ),
        )
        for service in health.services
    )
    activity, activity_partial = _dashboard_activity(db, health)
    return CustomerDashboardHighlights(
        greeting_name=greeting_name or "Customer",
        greeting_period=greeting_period,
        observed_at=health.as_of,
        account_credit=credit,
        services=services,
        support=_dashboard_support(db, health.account_id),
        activity=activity,
        activity_partial=activity_partial,
    )
