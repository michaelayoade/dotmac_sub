"""Owner of the prepaid enforcement threshold.

The prepaid enforcement gate asks one question: *how much balance must this
account hold before we stop suspending it?* This module owns that decision.

There is exactly one implementation of the rule — the batch one. The scalar
entry point is a thin adapter over it, so the enforcement sweep, the customer
portal projection, and the audit harness cannot drift apart. A second
implementation of a suspension threshold would let an audit tool disagree with
the enforcement it exists to check.

The access threshold is zero while every collectible prepaid service has exact
current coverage. A configured minimum is a reserve/top-up target, not a reason
to suspend service that is already paid for. When a service is due and has no
current coverage, the threshold is::

    max(configured_minimum, unfunded_renewal_requirement)

where ``configured_minimum`` is the account's ``min_balance`` override, falling
back to the canonical ``billing.prepaid_default_min_balance`` setting, and
``unfunded_renewal_requirement`` is the summed effective price of every due,
uncovered collectible prepaid subscription. Coverage classification belongs to
``financial.prepaid_service_coverage``. A future billing anchor without that
evidence is an unresolved projection and blocks adverse enforcement.

Query cost is bounded by the number of accounts *batches*, not by the number of
accounts: resolving 5,269 accounts costs the same handful of queries as resolving
250. The per-account form issued ~6 queries each, which made a full enforcement
audit ~16k statements and unfit to run against production.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import (
    BillingMode,
    OfferPrice,
    OfferVersionPrice,
    PriceType,
    Subscription,
)
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber
from app.services import settings_spec
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.domain_errors import DomainError
from app.services.prepaid_currency import (
    normalize_prepaid_currency,
    resolve_prepaid_enforcement_currency,
)

ZERO = Decimal("0.00")


class PrepaidThresholdError(DomainError):
    """Stable failure at the prepaid-threshold boundary."""


class PrepaidCurrencyMismatchError(PrepaidThresholdError):
    """A price would require comparing amounts in different currencies."""


class PriceRow(Protocol):
    created_at: datetime
    id: UUID
    amount: Decimal
    currency: str


PriceRowT = TypeVar("PriceRowT", bound=PriceRow)


@dataclass(frozen=True, slots=True)
class PrepaidThresholdDecision:
    """Currency-bound threshold with exact minimum and renewal provenance."""

    account_id: str
    configured_minimum: Decimal
    unfunded_renewal_requirement: Decimal
    currency: str
    covered_subscription_ids: tuple[UUID, ...] = ()
    actionable_uncovered_subscription_ids: tuple[UUID, ...] = ()
    unresolved_projection_subscription_ids: tuple[UUID, ...] = ()

    @property
    def threshold(self) -> Decimal:
        if not self.actionable_uncovered_subscription_ids:
            return ZERO
        return max(self.configured_minimum, self.unfunded_renewal_requirement)

    @property
    def top_up_target(self) -> Decimal:
        """Advisory reserve/renewal target, never a coverage override."""
        return max(self.configured_minimum, self.unfunded_renewal_requirement)


def _newest(rows: Iterable[PriceRowT]) -> PriceRowT | None:
    """Pick the newest price row the way the scalar resolver's ORDER BY does.

    ``_resolve_price`` orders by ``created_at DESC, id DESC`` and takes the
    first. Replicated exactly so a batch resolve cannot pick a different price
    than the enforcement path would.
    """
    newest = None
    for row in rows:
        if newest is None or (row.created_at, str(row.id)) > (
            newest.created_at,
            str(newest.id),
        ):
            newest = row
    return newest


def _minimum(value: object, *, source: str) -> Decimal:
    try:
        minimum = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PrepaidThresholdError(
            code="financial.prepaid_threshold.invalid_minimum_balance",
            message="The prepaid minimum balance must be a non-negative amount.",
            details={"source": source},
        ) from exc
    if not minimum.is_finite() or minimum < ZERO:
        raise PrepaidThresholdError(
            code="financial.prepaid_threshold.invalid_minimum_balance",
            message="The prepaid minimum balance must be a non-negative amount.",
            details={"source": source},
        )
    return minimum


def _decisions(
    account_ids: Sequence[str],
    *,
    configured: dict[str, Decimal],
    required: dict[str, Decimal],
    currency: str,
    covered: dict[str, tuple[UUID, ...]] | None = None,
    actionable: dict[str, tuple[UUID, ...]] | None = None,
    unresolved: dict[str, tuple[UUID, ...]] | None = None,
) -> dict[str, PrepaidThresholdDecision]:
    covered = covered or {}
    actionable = actionable or {}
    unresolved = unresolved or {}
    return {
        account_id: PrepaidThresholdDecision(
            account_id=account_id,
            configured_minimum=configured[account_id],
            unfunded_renewal_requirement=required.get(account_id, ZERO),
            currency=currency,
            covered_subscription_ids=covered.get(account_id, ()),
            actionable_uncovered_subscription_ids=actionable.get(account_id, ()),
            unresolved_projection_subscription_ids=unresolved.get(account_id, ()),
        )
        for account_id in account_ids
    }


def resolve_prepaid_threshold_decisions(
    db: Session,
    account_ids: Sequence[UUID | str],
    *,
    now: datetime | None = None,
    currency: str | None = None,
) -> dict[str, PrepaidThresholdDecision]:
    """Resolve the prepaid enforcement threshold for many accounts at once.

    This is the owner. Returns one typed provenance decision for every id;
    an account with no prepaid service has a zero renewal requirement.
    """
    from app.services.billing_automation import _effective_unit_price

    effective_now = now or datetime.now(UTC)
    enforcement_currency = (
        normalize_prepaid_currency(currency)
        if currency is not None
        else resolve_prepaid_enforcement_currency(db)
    )
    ids = [str(a) for a in account_ids]
    if not ids:
        return {}

    # 1. configured minimum: per-account override, else the domain default.
    default_raw = settings_spec.resolve_value(
        db, SettingDomain.billing, "prepaid_default_min_balance"
    )
    default_minimum = _minimum(
        default_raw if default_raw is not None else ZERO,
        source="billing.prepaid_default_min_balance",
    )
    configured: dict[str, Decimal] = {}
    for account_id, min_balance in db.execute(
        select(Subscriber.id, Subscriber.min_balance).where(Subscriber.id.in_(ids))
    ).all():
        account_key = str(account_id)
        configured[account_key] = (
            _minimum(min_balance, source=f"account:{account_key}")
            if min_balance is not None
            else default_minimum
        )
    missing_accounts = sorted(set(ids) - set(configured))
    if missing_accounts:
        raise PrepaidThresholdError(
            code="financial.prepaid_threshold.account_not_found",
            message="A prepaid threshold account was not found.",
            details={"account_ids": missing_accounts},
        )

    # 2. the collectible prepaid subscriptions those accounts hold.
    subscriptions = list(
        db.scalars(
            select(Subscription).where(
                Subscription.subscriber_id.in_(ids),
                Subscription.billing_mode == BillingMode.prepaid,
                Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES),
            )
        ).all()
    )
    if not subscriptions:
        return _decisions(
            ids,
            configured=configured,
            required={},
            currency=enforcement_currency,
        )

    # 3. Classify current service through the named coverage owner. A future
    # billing anchor without evidence is unresolved, not paid service.
    from app.services.prepaid_service_coverage import (
        PrepaidCoverageStatus,
        resolve_prepaid_service_coverage,
    )

    coverage = resolve_prepaid_service_coverage(
        db,
        subscriptions,
        as_of=effective_now,
    )
    covered_by_account: dict[str, list[UUID]] = defaultdict(list)
    actionable_by_account: dict[str, list[UUID]] = defaultdict(list)
    unresolved_by_account: dict[str, list[UUID]] = defaultdict(list)
    for subscription in subscriptions:
        account_key = str(subscription.subscriber_id)
        status = coverage[subscription.id].status
        if status == PrepaidCoverageStatus.covered:
            covered_by_account[account_key].append(subscription.id)
        elif status == PrepaidCoverageStatus.unresolved_projection:
            unresolved_by_account[account_key].append(subscription.id)
        else:
            actionable_by_account[account_key].append(subscription.id)

    # 4. Only genuinely due/uncovered services form an access requirement.
    # Unresolved projections are quarantined from adverse action.
    unfunded = [
        subscription
        for subscription in subscriptions
        if coverage[subscription.id].status == PrepaidCoverageStatus.uncovered_due
    ]
    if not unfunded:
        return _decisions(
            ids,
            configured=configured,
            required={},
            currency=enforcement_currency,
            covered={
                key: tuple(sorted(values, key=str))
                for key, values in covered_by_account.items()
            },
            unresolved={
                key: tuple(sorted(values, key=str))
                for key, values in unresolved_by_account.items()
            },
        )

    version_ids = {s.offer_version_id for s in unfunded if s.offer_version_id}
    offer_ids = {s.offer_id for s in unfunded if s.offer_id}

    version_prices: dict[str, list[OfferVersionPrice]] = defaultdict(list)
    if version_ids:
        for version_row in db.scalars(
            select(OfferVersionPrice).where(
                OfferVersionPrice.offer_version_id.in_(version_ids),
                OfferVersionPrice.price_type == PriceType.recurring,
                OfferVersionPrice.is_active.is_(True),
            )
        ).all():
            version_prices[str(version_row.offer_version_id)].append(version_row)

    offer_prices: dict[str, list[OfferPrice]] = defaultdict(list)
    if offer_ids:
        for offer_row in db.scalars(
            select(OfferPrice).where(
                OfferPrice.offer_id.in_(offer_ids),
                OfferPrice.price_type == PriceType.recurring,
                OfferPrice.is_active.is_(True),
            )
        ).all():
            offer_prices[str(offer_row.offer_id)].append(offer_row)

    # 5. sum the effective price of every due, uncovered subscription.
    required: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for subscription in unfunded:
        amount: Decimal | None = None
        amount_currency = enforcement_currency
        if subscription.offer_version_id:
            newest_version = _newest(
                version_prices.get(str(subscription.offer_version_id), [])
            )
            if newest_version is not None:
                amount = newest_version.amount
                amount_currency = str(newest_version.currency or "").strip().upper()
        if amount is None and subscription.offer_id:
            newest_offer = _newest(offer_prices.get(str(subscription.offer_id), []))
            if newest_offer is not None:
                amount = newest_offer.amount
                amount_currency = str(newest_offer.currency or "").strip().upper()
        if amount is None:
            amount = subscription.unit_price
        if amount is None:
            raise PrepaidThresholdError(
                code="financial.prepaid_threshold.missing_subscription_price",
                message=(
                    "An unfunded prepaid subscription has no effective recurring price."
                ),
                details={"subscription_id": str(subscription.id)},
            )
        if amount_currency != enforcement_currency:
            raise PrepaidCurrencyMismatchError(
                code="financial.prepaid_threshold.currency_mismatch",
                message=(
                    "A prepaid subscription price currency does not match the "
                    "enforcement currency."
                ),
                details={
                    "subscription_id": str(subscription.id),
                    "price_currency": amount_currency or None,
                    "enforcement_currency": enforcement_currency,
                },
            )
        effective = _effective_unit_price(subscription, amount, effective_now)
        if effective > ZERO:
            required[str(subscription.subscriber_id)] += effective

    return _decisions(
        ids,
        configured=configured,
        required=required,
        currency=enforcement_currency,
        covered={
            key: tuple(sorted(values, key=str))
            for key, values in covered_by_account.items()
        },
        actionable={
            key: tuple(sorted(values, key=str))
            for key, values in actionable_by_account.items()
        },
        unresolved={
            key: tuple(sorted(values, key=str))
            for key, values in unresolved_by_account.items()
        },
    )


def resolve_prepaid_thresholds(
    db: Session,
    account_ids: Sequence[UUID | str],
    *,
    now: datetime | None = None,
    currency: str | None = None,
) -> dict[str, Decimal]:
    """Return the scalar threshold projection for batch compatibility."""

    return {
        account_id: decision.threshold
        for account_id, decision in resolve_prepaid_threshold_decisions(
            db,
            account_ids,
            now=now,
            currency=currency,
        ).items()
    }


def resolve_prepaid_threshold_decision(
    db: Session,
    account: Subscriber,
    *,
    now: datetime | None = None,
    currency: str | None = None,
) -> PrepaidThresholdDecision:
    """Resolve the typed threshold outcome for one canonical account."""

    return resolve_prepaid_threshold_decisions(
        db,
        [account.id],
        now=now,
        currency=currency,
    )[str(account.id)]


def resolve_prepaid_threshold(
    db: Session,
    account: Subscriber,
    *,
    now: datetime | None = None,
    currency: str | None = None,
) -> Decimal:
    """Threshold for one account — a thin adapter over the batch owner.

    Deliberately delegates rather than reimplementing: one set of rules, so the
    enforcement sweep and any batch consumer cannot disagree.
    """
    return resolve_prepaid_threshold_decision(
        db,
        account,
        now=now,
        currency=currency,
    ).threshold
