"""Canonical customer chargeability classification for operational surfaces.

This resolver answers whether an account's current administrative service scope
is conclusively customer-billable, conclusively non-billable, or requires
pricing review.  A list filter may expose review work beside confirmed
non-billable accounts, but only an explicit zero catalog price or an effective
billing treatment suppresses customer billing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import and_, case, func, not_, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.catalog import (
    OfferPrice,
    OfferVersionPrice,
    PriceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber
from app.services.subscription_billing_treatments import (
    effective_customer_billing_treatment_clause,
)

CHARGEABILITY_SERVICE_STATUSES = (
    SubscriptionStatus.pending,
    SubscriptionStatus.active,
    SubscriptionStatus.blocked,
    SubscriptionStatus.suspended,
    SubscriptionStatus.stopped,
    SubscriptionStatus.disabled,
)


class CustomerChargeabilityStatus(StrEnum):
    confirmed_non_billable = "confirmed_non_billable"
    review_required = "review_required"
    billable = "billable"
    no_current_service = "no_current_service"


class ChargeabilityReason(StrEnum):
    active_billing_treatment = "active_billing_treatment"
    explicit_zero_price = "explicit_zero_price"
    missing_catalog_price = "missing_catalog_price"
    multiple_active_catalog_prices = "multiple_active_catalog_prices"
    catalog_subscription_price_mismatch = "catalog_subscription_price_mismatch"
    chargeable_service = "chargeable_service"
    no_current_service = "no_current_service"


@dataclass(frozen=True, slots=True)
class SubscriptionChargeability:
    subscription_id: UUID
    status: SubscriptionStatus
    catalog_amount: Decimal | None
    subscription_amount: Decimal | None
    reason: ChargeabilityReason

    @property
    def requires_review(self) -> bool:
        return self.reason in {
            ChargeabilityReason.missing_catalog_price,
            ChargeabilityReason.multiple_active_catalog_prices,
            ChargeabilityReason.catalog_subscription_price_mismatch,
        }

    @property
    def is_non_billable(self) -> bool:
        return self.reason in {
            ChargeabilityReason.active_billing_treatment,
            ChargeabilityReason.explicit_zero_price,
        }


@dataclass(frozen=True, slots=True)
class CustomerChargeability:
    account_id: UUID
    status: CustomerChargeabilityStatus
    reasons: tuple[ChargeabilityReason, ...]
    subscriptions: tuple[SubscriptionChargeability, ...]

    @property
    def appears_in_non_billable_section(self) -> bool:
        return self.status in {
            CustomerChargeabilityStatus.confirmed_non_billable,
            CustomerChargeabilityStatus.review_required,
        }


def _catalog_price_expressions():
    version_amount = (
        select(OfferVersionPrice.amount)
        .where(
            OfferVersionPrice.offer_version_id == Subscription.offer_version_id,
            OfferVersionPrice.price_type == PriceType.recurring,
            OfferVersionPrice.is_active.is_(True),
        )
        .order_by(OfferVersionPrice.created_at.desc(), OfferVersionPrice.id.desc())
        .limit(1)
        .correlate(Subscription)
        .scalar_subquery()
    )
    offer_amount = (
        select(OfferPrice.amount)
        .where(
            OfferPrice.offer_id == Subscription.offer_id,
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
        .order_by(OfferPrice.created_at.desc(), OfferPrice.id.desc())
        .limit(1)
        .correlate(Subscription)
        .scalar_subquery()
    )
    version_count = (
        select(func.count(OfferVersionPrice.id))
        .where(
            OfferVersionPrice.offer_version_id == Subscription.offer_version_id,
            OfferVersionPrice.price_type == PriceType.recurring,
            OfferVersionPrice.is_active.is_(True),
        )
        .correlate(Subscription)
        .scalar_subquery()
    )
    offer_count = (
        select(func.count(OfferPrice.id))
        .where(
            OfferPrice.offer_id == Subscription.offer_id,
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
        .correlate(Subscription)
        .scalar_subquery()
    )
    catalog_amount = func.coalesce(version_amount, offer_amount)
    active_price_count = case(
        (version_count > 0, version_count),
        else_=offer_count,
    )
    return catalog_amount, active_price_count


def _price_review_clause() -> ColumnElement[bool]:
    catalog_amount, active_price_count = _catalog_price_expressions()
    return or_(
        catalog_amount.is_(None),
        active_price_count > 1,
        and_(catalog_amount == 0, Subscription.unit_price > 0),
        and_(
            catalog_amount > 0,
            Subscription.unit_price.is_not(None),
            Subscription.unit_price <= 0,
        ),
    )


def _genuinely_free_clause() -> ColumnElement[bool]:
    catalog_amount, active_price_count = _catalog_price_expressions()
    return and_(
        active_price_count == 1,
        catalog_amount == 0,
        or_(Subscription.unit_price.is_(None), Subscription.unit_price <= 0),
    )


def confirmed_non_billable_customer_clause() -> ColumnElement[bool]:
    """SQL predicate for accounts conclusively free across current services."""

    treatment = effective_customer_billing_treatment_clause()
    review = _price_review_clause()
    suppressed = or_(treatment, _genuinely_free_clause())
    has_service = (
        select(Subscription.id)
        .where(
            Subscription.subscriber_id == Subscriber.id,
            Subscription.status.in_(CHARGEABILITY_SERVICE_STATUSES),
        )
        .correlate(Subscriber)
        .exists()
    )
    has_review = (
        select(Subscription.id)
        .where(
            Subscription.subscriber_id == Subscriber.id,
            Subscription.status.in_(CHARGEABILITY_SERVICE_STATUSES),
            review,
        )
        .correlate(Subscriber)
        .exists()
    )
    has_chargeable = (
        select(Subscription.id)
        .where(
            Subscription.subscriber_id == Subscriber.id,
            Subscription.status.in_(CHARGEABILITY_SERVICE_STATUSES),
            not_(suppressed),
        )
        .correlate(Subscriber)
        .exists()
    )
    return and_(has_service, not_(has_review), not_(has_chargeable))


def chargeability_review_required_customer_clause() -> ColumnElement[bool]:
    """SQL predicate for accounts with incomplete or contradictory prices."""

    return (
        select(Subscription.id)
        .where(
            Subscription.subscriber_id == Subscriber.id,
            Subscription.status.in_(CHARGEABILITY_SERVICE_STATUSES),
            _price_review_clause(),
        )
        .correlate(Subscriber)
        .exists()
    )


def non_billable_section_customer_clause() -> ColumnElement[bool]:
    """Operational section: confirmed non-billable plus pricing review work."""

    return or_(
        confirmed_non_billable_customer_clause(),
        chargeability_review_required_customer_clause(),
    )


def _subscription_reason(
    *,
    catalog_amount: Decimal | None,
    active_price_count: int,
    subscription_amount: Decimal | None,
    treatment_active: bool,
) -> ChargeabilityReason:
    if catalog_amount is None:
        return ChargeabilityReason.missing_catalog_price
    if active_price_count > 1:
        return ChargeabilityReason.multiple_active_catalog_prices
    if (
        catalog_amount == 0
        and subscription_amount is not None
        and subscription_amount > 0
    ) or (
        catalog_amount > 0
        and subscription_amount is not None
        and subscription_amount <= 0
    ):
        return ChargeabilityReason.catalog_subscription_price_mismatch
    if treatment_active:
        return ChargeabilityReason.active_billing_treatment
    if catalog_amount == 0 and (
        subscription_amount is None or subscription_amount <= 0
    ):
        return ChargeabilityReason.explicit_zero_price
    return ChargeabilityReason.chargeable_service


def resolve_customer_chargeability(
    db: Session,
    account_ids: tuple[UUID, ...],
) -> dict[UUID, CustomerChargeability]:
    """Resolve typed chargeability for a bounded account cohort."""

    if not account_ids:
        return {}
    catalog_amount, active_price_count = _catalog_price_expressions()
    treatment_active = effective_customer_billing_treatment_clause()
    rows = db.execute(
        select(
            Subscription.subscriber_id,
            Subscription.id,
            Subscription.status,
            Subscription.unit_price,
            catalog_amount.label("catalog_amount"),
            active_price_count.label("active_price_count"),
            treatment_active.label("treatment_active"),
        )
        .where(
            Subscription.subscriber_id.in_(account_ids),
            Subscription.status.in_(CHARGEABILITY_SERVICE_STATUSES),
        )
        .order_by(Subscription.subscriber_id, Subscription.id)
    ).all()
    by_account: dict[UUID, list[SubscriptionChargeability]] = {
        account_id: [] for account_id in account_ids
    }
    for row in rows:
        amount = Decimal(row.catalog_amount) if row.catalog_amount is not None else None
        unit_price = Decimal(row.unit_price) if row.unit_price is not None else None
        reason = _subscription_reason(
            catalog_amount=amount,
            active_price_count=int(row.active_price_count or 0),
            subscription_amount=unit_price,
            treatment_active=bool(row.treatment_active),
        )
        by_account[row.subscriber_id].append(
            SubscriptionChargeability(
                subscription_id=row.id,
                status=row.status,
                catalog_amount=amount,
                subscription_amount=unit_price,
                reason=reason,
            )
        )

    outcomes: dict[UUID, CustomerChargeability] = {}
    for account_id in account_ids:
        services = tuple(by_account[account_id])
        if not services:
            status = CustomerChargeabilityStatus.no_current_service
            reasons = (ChargeabilityReason.no_current_service,)
        elif any(item.requires_review for item in services):
            status = CustomerChargeabilityStatus.review_required
            reasons = tuple(
                dict.fromkeys(item.reason for item in services if item.requires_review)
            )
        elif all(item.is_non_billable for item in services):
            status = CustomerChargeabilityStatus.confirmed_non_billable
            reasons = tuple(dict.fromkeys(item.reason for item in services))
        else:
            status = CustomerChargeabilityStatus.billable
            reasons = tuple(dict.fromkeys(item.reason for item in services))
        outcomes[account_id] = CustomerChargeability(
            account_id=account_id,
            status=status,
            reasons=reasons,
            subscriptions=services,
        )
    return outcomes


__all__ = [
    "CHARGEABILITY_SERVICE_STATUSES",
    "ChargeabilityReason",
    "CustomerChargeability",
    "CustomerChargeabilityStatus",
    "SubscriptionChargeability",
    "chargeability_review_required_customer_clause",
    "confirmed_non_billable_customer_clause",
    "non_billable_section_customer_clause",
    "resolve_customer_chargeability",
]
