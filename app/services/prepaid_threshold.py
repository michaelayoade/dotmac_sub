"""Owner of the prepaid enforcement threshold.

The prepaid enforcement gate asks one question: *how much balance must this
account hold before we stop suspending it?* This module owns that decision.

There is exactly one implementation of the rule — the batch one. The scalar
entry point is a thin adapter over it, so the enforcement sweep, the customer
portal projection, and the audit harness cannot drift apart. A second
implementation of a suspension threshold would let an audit tool disagree with
the enforcement it exists to check.

The threshold is::

    max(configured_minimum, unfunded_renewal_requirement)

where ``configured_minimum`` is the account's ``min_balance`` override, falling
back to the ``prepaid_default_min_balance`` setting, and
``unfunded_renewal_requirement`` is the summed effective price of every
collectible prepaid subscription that has **no current paid coverage** — paid
coverage being an active ``ServiceEntitlement`` spanning ``now``, or (legacy
fallback, while cutover-era invoices are reconciled into explicit entitlement
rows) a paid invoice whose billing period spans ``now``.

Query cost is bounded by the number of accounts *batches*, not by the number of
accounts: resolving 5,269 accounts costs the same handful of queries as resolving
250. The per-account form issued ~6 queries each, which made a full enforcement
audit ~16k statements and unfit to run against production.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
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

ZERO = Decimal("0.00")


def _newest(rows: Iterable[Any]) -> Any | None:
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


def resolve_prepaid_thresholds(
    db: Session,
    account_ids: Sequence[Any],
    *,
    now: datetime | None = None,
) -> dict[str, Decimal]:
    """Resolve the prepaid enforcement threshold for many accounts at once.

    This is the owner. Returns ``{account_id: threshold}`` for every id given;
    an account with no prepaid service resolves to its configured minimum.
    """
    from app.services.billing_automation import _effective_unit_price

    effective_now = now or datetime.now(UTC)
    ids = [str(a) for a in account_ids]
    if not ids:
        return {}

    # 1. configured minimum: per-account override, else the domain default.
    default_raw = settings_spec.resolve_value(
        db, SettingDomain.collections, "prepaid_default_min_balance"
    )
    default_minimum = Decimal(str(default_raw)) if default_raw is not None else ZERO
    configured: dict[str, Decimal] = {}
    for account_id, min_balance in db.execute(
        select(Subscriber.id, Subscriber.min_balance).where(Subscriber.id.in_(ids))
    ).all():
        configured[str(account_id)] = (
            Decimal(str(min_balance)) if min_balance is not None else default_minimum
        )
    for account_id in ids:
        configured.setdefault(account_id, default_minimum)

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
        return {account_id: configured[account_id] for account_id in ids}

    subscription_ids = [s.id for s in subscriptions]

    # 3. current paid coverage — an active entitlement spanning now.
    covered: set[str] = {
        str(subscription_id)
        for (subscription_id,) in db.execute(
            select(ServiceEntitlement.subscription_id).where(
                ServiceEntitlement.subscription_id.in_(subscription_ids),
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
                ServiceEntitlement.starts_at <= effective_now,
                ServiceEntitlement.ends_at > effective_now,
            )
        ).all()
    }

    # 4. legacy fallback: a paid invoice whose billing period spans now. Only for
    #    the subscriptions that no entitlement row covers, matching the scalar
    #    resolver, which consults this only when the entitlement lookup misses.
    uncovered = [s.id for s in subscriptions if str(s.id) not in covered]
    if uncovered:
        covered.update(
            str(subscription_id)
            for (subscription_id,) in db.execute(
                select(InvoiceLine.subscription_id)
                .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
                .where(
                    InvoiceLine.subscription_id.in_(uncovered),
                    InvoiceLine.is_active.is_(True),
                    Invoice.is_active.is_(True),
                    Invoice.status == InvoiceStatus.paid,
                    Invoice.billing_period_start.isnot(None),
                    Invoice.billing_period_start <= effective_now,
                    Invoice.billing_period_end.isnot(None),
                    Invoice.billing_period_end > effective_now,
                )
                .distinct()
            ).all()
        )

    # 5. pricing inputs, fetched once per distinct offer / offer-version.
    unfunded = [s for s in subscriptions if str(s.id) not in covered]
    if not unfunded:
        return {account_id: configured[account_id] for account_id in ids}

    version_ids = {s.offer_version_id for s in unfunded if s.offer_version_id}
    offer_ids = {s.offer_id for s in unfunded if s.offer_id}

    version_prices: dict[str, list[Any]] = defaultdict(list)
    if version_ids:
        for version_row in db.scalars(
            select(OfferVersionPrice).where(
                OfferVersionPrice.offer_version_id.in_(version_ids),
                OfferVersionPrice.price_type == PriceType.recurring,
                OfferVersionPrice.is_active.is_(True),
            )
        ).all():
            version_prices[str(version_row.offer_version_id)].append(version_row)

    offer_prices: dict[str, list[Any]] = defaultdict(list)
    if offer_ids:
        for offer_row in db.scalars(
            select(OfferPrice).where(
                OfferPrice.offer_id.in_(offer_ids),
                OfferPrice.price_type == PriceType.recurring,
                OfferPrice.is_active.is_(True),
            )
        ).all():
            offer_prices[str(offer_row.offer_id)].append(offer_row)

    # 6. sum the effective price of every unfunded prepaid subscription.
    required: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for subscription in unfunded:
        amount: Decimal | None = None
        if subscription.offer_version_id:
            newest = _newest(version_prices.get(str(subscription.offer_version_id), []))
            if newest is not None:
                amount = newest.amount
        if amount is None and subscription.offer_id:
            newest = _newest(offer_prices.get(str(subscription.offer_id), []))
            if newest is not None:
                amount = newest.amount
        if amount is None:
            amount = subscription.unit_price
        if amount is None:
            continue
        effective = _effective_unit_price(subscription, amount, effective_now)
        if effective > ZERO:
            required[str(subscription.subscriber_id)] += effective

    return {
        account_id: max(configured[account_id], required.get(account_id, ZERO))
        for account_id in ids
    }


def resolve_prepaid_threshold(
    db: Session,
    account: Subscriber,
    *,
    now: datetime | None = None,
) -> Decimal:
    """Threshold for one account — a thin adapter over the batch owner.

    Deliberately delegates rather than reimplementing: one set of rules, so the
    enforcement sweep and any batch consumer cannot disagree.
    """
    resolved = resolve_prepaid_thresholds(db, [account.id], now=now)
    return resolved.get(str(account.id), ZERO)
