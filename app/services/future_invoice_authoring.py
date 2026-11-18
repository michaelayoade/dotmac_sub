"""Read-only catalogue selection and fingerprinted forward invoice evidence.

The administrative draft owner calls this resolver inside its own transaction;
this module never stages invoices, payments, access or service coverage.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from dotmac_kernel.money import Currency, Money
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, TaxApplication, TaxRate
from app.models.catalog import (
    AddOn,
    AddOnPrice,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferPrice,
    OfferVersionPrice,
    PriceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
    UsageAllowanceResetBasis,
)
from app.models.customer_tax_policy import CustomerTaxPolicy
from app.models.subscriber import Address, Subscriber
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.models.subscription_lifecycle_schedule import (
    SubscriptionLifecycleSchedule,
    SubscriptionLifecycleScheduleStatus,
)
from app.services.billing.cadence import service_period
from app.services.billing.invoices import (
    invoice_period_conflict,
    service_period_charge,
)
from app.services.billing_automation import (
    _period_end,
    preview_postpaid_recurring_charge,
)
from app.services.billing_tax_resolution import (
    StandaloneCatalogueTaxQuery,
    resolve_standalone_catalogue_tax,
    resolve_subscription_tax,
)
from app.services.domain_errors import DomainError
from app.services.prepaid_period_purchases import _monthly_cadence
from app.services.prepaid_service_renewals import (
    PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES,
    PrepaidSubscriptionSettlementPeriodQuery,
    resolve_prepaid_monthly_charge_detail,
    resolve_prepaid_subscription_settlement_period,
)


class CatalogueItemKind(StrEnum):
    offer = "offer"
    add_on = "add_on"


class FutureInvoicePeriodKind(StrEnum):
    one_time = "one_time"
    monthly = "monthly"
    capped_pending_renewal = "capped_pending_renewal"


@dataclass(frozen=True, slots=True)
class CatalogueQuery:
    account_id: UUID
    currency: str = "NGN"
    search: str = ""
    limit: int = 20


@dataclass(frozen=True, slots=True)
class CatalogueChoice:
    item_kind: CatalogueItemKind
    item_id: UUID
    price_id: UUID
    name: str
    price_type: PriceType
    amount: Decimal
    currency: str
    tax_application: TaxApplication


@dataclass(frozen=True, slots=True)
class SubscriptionChoicesQuery:
    account_id: UUID
    offer_id: UUID


@dataclass(frozen=True, slots=True)
class SubscriptionChoice:
    subscription_id: UUID
    offer_id: UUID
    label: str
    billing_mode: BillingMode


@dataclass(frozen=True, slots=True)
class FutureInvoicePeriodsQuery:
    account_id: UUID
    subscription_id: UUID
    as_of: datetime
    horizon: int = 6
    invoice_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class FutureInvoicePeriodChoice:
    period_kind: FutureInvoicePeriodKind
    starts_at: datetime | None
    ends_at: datetime | None
    label: str
    available: bool
    reason: str | None = None
    conflict_invoice_id: UUID | None = None
    conflict_kind: str | None = None


@dataclass(frozen=True, slots=True)
class PreviewFutureInvoiceLineQuery:
    account_id: UUID
    currency: str
    item_kind: CatalogueItemKind
    item_id: UUID
    price_id: UUID
    as_of: datetime
    quantity: Decimal = Decimal("1")
    subscription_id: UUID | None = None
    period_start: datetime | None = None
    invoice_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class FutureInvoiceLineSelection:
    query: PreviewFutureInvoiceLineQuery
    fingerprint: str


@dataclass(frozen=True, slots=True)
class FutureInvoiceLinePreview:
    description: str
    quantity: Decimal
    unit_price: Decimal
    tax_rate_id: UUID | None
    tax_application: TaxApplication
    tax_total: Decimal
    total: Decimal
    subscription_id: UUID | None
    starts_at: datetime | None
    ends_at: datetime | None
    period_kind: FutureInvoicePeriodKind
    selection: FutureInvoiceLineSelection
    allowance_gb: Decimal | None = None
    validity_days: int | None = None
    tax_source: str | None = None
    customer_tax_policy_version: int | None = None


class FutureInvoiceAuthoringError(DomainError):
    pass


def _error(code: str, message: str) -> FutureInvoiceAuthoringError:
    return FutureInvoiceAuthoringError(
        code=f"financial.invoice_draft_authoring.{code}",
        message=message,
        retryable=False,
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _error("invalid_preview_date", "Preview dates must include a timezone.")
    return value.astimezone(UTC)


def _refresh_source(db: Session, source: object) -> None:
    """Observe a lock wait's committed result without discarding staged writes."""
    if (
        source in db.new
        or source in db.deleted
        or db.is_modified(source, include_collections=True)
    ):
        raise _error(
            "source_changes_pending",
            "Save pending catalogue or customer changes before reviewing invoice lines.",
        )
    db.refresh(source)


def _refresh_tax_sources(db: Session, account_id: UUID) -> None:
    for policy in db.scalars(
        select(CustomerTaxPolicy).where(CustomerTaxPolicy.account_id == account_id)
    ).all():
        _refresh_source(db, policy)
    for rate in db.scalars(select(TaxRate).where(TaxRate.is_active.is_(True))).all():
        _refresh_source(db, rate)


def _account(db: Session, account_id: UUID) -> Subscriber:
    account = db.get(Subscriber, account_id)
    if account is None:
        raise _error("account_not_found", "Choose an existing customer account.")
    _refresh_source(db, account)
    return account


def _subscription(db: Session, account_id: UUID, subscription_id: UUID) -> Subscription:
    row = db.get(Subscription, subscription_id)
    if row is not None:
        _refresh_source(db, row)
        db.expire(row, ["offer", "offer_version"])
        if row.offer is not None:
            _refresh_source(db, row.offer)
            db.expire(row.offer, ["usage_allowance"])
        if row.offer_version is not None:
            _refresh_source(db, row.offer_version)
            db.expire(row.offer_version, ["usage_allowance"])
        if row.service_address_id is not None:
            address = db.get(Address, row.service_address_id)
            if address is not None:
                _refresh_source(db, address)
        for price in db.scalars(
            select(OfferPrice).where(OfferPrice.offer_id == row.offer_id)
        ).all():
            _refresh_source(db, price)
        if row.offer_version_id is not None:
            for version_price in db.scalars(
                select(OfferVersionPrice).where(
                    OfferVersionPrice.offer_version_id == row.offer_version_id
                )
            ).all():
                _refresh_source(db, version_price)
    if row is None or row.subscriber_id != account_id:
        raise _error(
            "subscription_account_mismatch",
            "Choose a subscription belonging to this customer.",
        )
    account = _account(db, account_id)
    if row.billing_mode is not account.billing_mode:
        raise _error(
            "billing_mode_mismatch",
            "The subscription and customer have different billing modes; reconcile them before invoicing.",
        )
    eligible = (
        row.status in PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES
        if row.billing_mode is BillingMode.prepaid
        else row.status is SubscriptionStatus.active
    )
    if not eligible:
        raise _error(
            "subscription_not_active",
            "This subscription state cannot be invoiced for future service.",
        )
    pending_change = db.scalar(
        select(SubscriptionChangeRequest.id)
        .where(
            SubscriptionChangeRequest.subscription_id == row.id,
            SubscriptionChangeRequest.is_active.is_(True),
            SubscriptionChangeRequest.applied_at.is_(None),
            SubscriptionChangeRequest.status.in_(
                [SubscriptionChangeStatus.pending, SubscriptionChangeStatus.approved]
            ),
        )
        .limit(1)
    )
    pending_lifecycle = db.scalar(
        select(SubscriptionLifecycleSchedule.id)
        .where(
            SubscriptionLifecycleSchedule.subscription_id == row.id,
            SubscriptionLifecycleSchedule.status.in_(
                [
                    SubscriptionLifecycleScheduleStatus.pending,
                    SubscriptionLifecycleScheduleStatus.processing,
                ]
            ),
        )
        .limit(1)
    )
    if pending_change is not None or pending_lifecycle is not None:
        raise _error(
            "pending_service_change",
            "Resolve scheduled service changes before selecting future invoice periods.",
        )
    return row


def catalogue_currencies(db: Session) -> tuple[str, ...]:
    """Read actual eligible catalogue denominations; never invent currencies."""
    offers = db.scalars(
        select(OfferPrice.currency)
        .join(CatalogOffer, OfferPrice.offer_id == CatalogOffer.id)
        .where(
            CatalogOffer.is_active.is_(True),
            OfferPrice.is_active.is_(True),
            OfferPrice.amount > 0,
            OfferPrice.price_type.in_([PriceType.one_time, PriceType.recurring]),
        )
        .distinct()
    )
    extras = db.scalars(
        select(AddOnPrice.currency)
        .join(AddOn, AddOnPrice.add_on_id == AddOn.id)
        .where(
            AddOn.is_active.is_(True),
            AddOnPrice.is_active.is_(True),
            AddOnPrice.amount > 0,
            AddOnPrice.price_type == PriceType.one_time,
        )
        .distinct()
    )
    return tuple(sorted({value.upper() for value in (*offers, *extras)}))


def catalogue_choices(
    db: Session, query: CatalogueQuery
) -> tuple[CatalogueChoice, ...]:
    """Return bounded, active priced records; quantity and tax remain server-owned."""
    _account(db, query.account_id)
    limit = min(50, max(1, query.limit))
    search = query.search.strip().lower()
    result: list[CatalogueChoice] = []
    pairs = (
        (CatalogueItemKind.offer, CatalogOffer, OfferPrice, OfferPrice.offer_id),
        (CatalogueItemKind.add_on, AddOn, AddOnPrice, AddOnPrice.add_on_id),
    )
    for kind, item_model, price_model, foreign_key in pairs:
        statement = (
            select(item_model, price_model)
            .join(price_model, foreign_key == item_model.id)
            .where(
                item_model.is_active.is_(True),
                price_model.is_active.is_(True),
                price_model.currency == query.currency.upper(),
                price_model.price_type.in_([PriceType.one_time, PriceType.recurring]),
                price_model.amount > 0,
            )
        )
        if search:
            statement = statement.where(item_model.name.ilike(f"%{search}%"))
        if kind is CatalogueItemKind.add_on:
            statement = statement.where(price_model.price_type == PriceType.one_time)
        for item, price in db.execute(
            statement.order_by(item_model.name, price_model.id).limit(limit)
        ).all():
            result.append(
                CatalogueChoice(
                    kind,
                    item.id,
                    price.id,
                    item.name,
                    price.price_type,
                    price.amount,
                    price.currency,
                    price.tax_application,
                )
            )
    return tuple(
        sorted(result, key=lambda row: (row.name.casefold(), str(row.price_id)))[:limit]
    )


def subscriptions_for_offer(
    db: Session, query: SubscriptionChoicesQuery
) -> tuple[SubscriptionChoice, ...]:
    account = _account(db, query.account_id)
    rows = db.scalars(
        select(Subscription)
        .where(
            Subscription.subscriber_id == query.account_id,
            Subscription.offer_id == query.offer_id,
            Subscription.billing_mode == account.billing_mode,
            Subscription.status.in_(
                [SubscriptionStatus.active, SubscriptionStatus.blocked]
            ),
        )
        .order_by(Subscription.id)
        .limit(100)
    ).all()
    return tuple(
        SubscriptionChoice(
            row.id,
            row.offer_id,
            f"{row.service_description or row.offer.name} · {str(row.id)[:8]}",
            row.billing_mode,
        )
        for row in rows
    )


def _is_capped(db: Session, subscription: Subscription) -> bool:
    allowance = (
        subscription.offer_version.usage_allowance
        if subscription.offer_version and subscription.offer_version.usage_allowance_id
        else subscription.offer.usage_allowance
    )
    if allowance is not None:
        _refresh_source(db, allowance)
    if allowance is not None and not allowance.is_active:
        raise _error(
            "capped_policy_unavailable",
            "The service usage policy is inactive; review it before invoicing.",
        )
    return bool(
        allowance and allowance.reset_basis is UsageAllowanceResetBasis.renewal_cycle
    )


def future_period_choices(
    db: Session, query: FutureInvoicePeriodsQuery
) -> tuple[FutureInvoicePeriodChoice, ...]:
    """Resolve bounded forward periods from incumbent date owners, never signup guesses."""
    as_of = _aware(query.as_of)
    row = _subscription(db, query.account_id, query.subscription_id)
    if _is_capped(db, row):
        if row.billing_mode is not BillingMode.prepaid:
            raise _error(
                "unsupported_capped_mode",
                "Capped renewals require a prepaid subscription.",
            )
        choice = FutureInvoicePeriodChoice(
            FutureInvoicePeriodKind.capped_pending_renewal,
            None,
            None,
            "One quota renewal · validity starts after verified settlement",
            True,
        )
        return (_with_period_conflict(db, query=query, choice=choice),)
    from app.services.catalog.subscriptions import _resolve_billing_cycle

    cycle = _resolve_billing_cycle(
        db,
        str(row.offer_id),
        str(row.offer_version_id) if row.offer_version_id else None,
        override=row.billing_cycle,
    )
    if cycle is not BillingCycle.monthly:
        raise _error(
            "unsupported_cadence",
            "This flow currently supports monthly service periods only.",
        )
    if row.billing_mode is BillingMode.prepaid:
        decision = resolve_prepaid_subscription_settlement_period(
            db,
            PrepaidSubscriptionSettlementPeriodQuery(
                subscription_id=row.id,
                account_id=query.account_id,
                effective_at=as_of,
                billing_cycle=BillingCycle.monthly,
            ),
        )
        if decision.covered_through is None or decision.period.starts_at < as_of:
            return (
                FutureInvoicePeriodChoice(
                    FutureInvoicePeriodKind.monthly,
                    None,
                    None,
                    "No verified future renewal boundary",
                    False,
                    "Current service coverage must be reconciled before reserving dated future service.",
                ),
            )
        start = decision.period.starts_at
    else:
        preview = preview_postpaid_recurring_charge(
            db, subscription_id=row.id, as_of=as_of
        )
        if preview.billing_cycle is not BillingCycle.monthly:
            raise _error(
                "unsupported_cadence",
                "This flow currently supports monthly service periods only.",
            )
        start = (
            preview.period_start
            if preview.period_start >= as_of
            else preview.period_end
        )
    result: list[FutureInvoicePeriodChoice] = []
    for index in range(min(12, max(1, query.horizon))):
        if row.billing_mode is BillingMode.prepaid:
            period = service_period(
                cadence=_monthly_cadence(), contract_start=start, index=index
            )
        else:
            from app.services.billing.cadence import Interval

            period = Interval(
                starts_at=start, ends_at=_period_end(start, BillingCycle.monthly)
            )
            start = period.ends_at
        if row.end_at is not None and _aware(row.end_at) < period.ends_at:
            break
        result.append(
            FutureInvoicePeriodChoice(
                FutureInvoicePeriodKind.monthly,
                period.starts_at,
                period.ends_at,
                f"{period.starts_at.astimezone(_monthly_cadence().zone()).date()} → {period.ends_at.astimezone(_monthly_cadence().zone()).date()}",
                True,
            )
        )
    return tuple(
        _with_period_conflict(db, query=query, choice=choice) for choice in result
    )


def _with_period_conflict(
    db: Session, *, query: FutureInvoicePeriodsQuery, choice: FutureInvoicePeriodChoice
) -> FutureInvoicePeriodChoice:
    from dataclasses import replace

    metadata: dict[str, object] = {
        "future_catalogue_invoice": True,
        "kind": "base_subscription",
        "future_invoice": {"period_kind": choice.period_kind.value},
    }
    if choice.starts_at is not None and choice.ends_at is not None:
        metadata.update(
            billing_period_start=choice.starts_at.isoformat(),
            billing_period_end=choice.ends_at.isoformat(),
        )
    charge = service_period_charge(
        subscription_id=query.subscription_id,
        metadata=metadata,
        header_starts_at=None,
        header_ends_at=None,
    )
    assert charge is not None
    conflict = invoice_period_conflict(
        db,
        account_id=query.account_id,
        charge=charge,
        exclude_invoice_id=query.invoice_id,
    )
    if conflict is not None:
        return replace(
            choice,
            available=False,
            reason=conflict.message,
            conflict_invoice_id=conflict.invoice_id,
            conflict_kind=conflict.reason,
        )
    addon_query = (
        select(SubscriptionAddOn.id)
        .join(AddOnPrice, AddOnPrice.add_on_id == SubscriptionAddOn.add_on_id)
        .where(
            SubscriptionAddOn.subscription_id == query.subscription_id,
            AddOnPrice.price_type == PriceType.recurring,
            AddOnPrice.is_active.is_(True),
            or_(
                SubscriptionAddOn.end_at.is_(None),
                SubscriptionAddOn.end_at > (choice.starts_at or query.as_of),
            ),
        )
    )
    if choice.ends_at is not None:
        addon_query = addon_query.where(
            or_(
                SubscriptionAddOn.start_at.is_(None),
                SubscriptionAddOn.start_at < choice.ends_at,
            )
        )
    if db.scalar(addon_query.limit(1)) is not None:
        return replace(
            choice,
            available=False,
            reason="This service has recurring add-ons in this period. Use the existing billing flow until all recurring components can be reviewed together.",
            conflict_kind="recurring_components_unsupported",
        )
    return choice


def _price(
    db: Session, query: PreviewFutureInvoiceLineQuery
) -> tuple[CatalogueOfferLike, CataloguePriceLike]:
    item: CatalogueOfferLike | None
    price: CataloguePriceLike | None
    if query.item_kind is CatalogueItemKind.offer:
        item = db.get(CatalogOffer, query.item_id)
        price = db.get(OfferPrice, query.price_id)
        matches = price is not None and price.offer_id == query.item_id
    else:
        item = db.get(AddOn, query.item_id)
        price = db.get(AddOnPrice, query.price_id)
        matches = price is not None and price.add_on_id == query.item_id
    if item is not None:
        _refresh_source(db, item)
    if price is not None:
        _refresh_source(db, price)
    matches = (
        (isinstance(price, OfferPrice) and price.offer_id == query.item_id)
        if query.item_kind is CatalogueItemKind.offer
        else (isinstance(price, AddOnPrice) and price.add_on_id == query.item_id)
    )
    if (
        item is None
        or price is None
        or not matches
        or not item.is_active
        or not price.is_active
        or price.amount <= 0
        or price.currency.upper() != query.currency.upper()
    ):
        raise _error(
            "catalogue_price_unavailable",
            "The selected catalogue price is no longer available in this currency.",
        )
    return item, price


CatalogueOfferLike = CatalogOffer | AddOn
CataloguePriceLike = OfferPrice | AddOnPrice


def _fingerprint(value: object) -> str:
    def encode(item: object) -> str:
        if isinstance(item, datetime):
            return _aware(item).isoformat()
        if isinstance(item, (UUID, Decimal, StrEnum)):
            return str(item)
        raise TypeError(f"Unsupported fingerprint value {type(item).__name__}")

    return hashlib.sha256(
        json.dumps(
            value, default=encode, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def preview_future_invoice_line(
    db: Session, query: PreviewFutureInvoiceLineQuery
) -> FutureInvoiceLinePreview:
    account = _account(db, query.account_id)
    _refresh_tax_sources(db, account.id)
    as_of = _aware(query.as_of)
    if (
        not query.quantity.is_finite()
        or query.quantity <= 0
        or query.quantity > Decimal("999999999.999")
        or query.quantity != query.quantity.quantize(Decimal("0.001"))
    ):
        raise _error(
            "quantity_invalid",
            "Quantity must be positive with at most three decimal places.",
        )
    item, price = _price(db, query)
    subscription = None
    starts_at = ends_at = None
    kind = FutureInvoicePeriodKind.one_time
    allowance_gb: Decimal | None = None
    validity_days: int | None = None
    amount = Decimal(price.amount)
    application = price.tax_application
    rate_id: UUID | None = None
    tax_rate = Decimal("0")
    provenance: dict[str, str | int | None] = {}
    if price.price_type is PriceType.recurring:
        if (
            query.item_kind is not CatalogueItemKind.offer
            or query.subscription_id is None
            or query.quantity != 1
        ):
            raise _error(
                "service_selection_required",
                "A monthly line requires this customer's subscription and quantity one.",
            )
        subscription = _subscription(db, account.id, query.subscription_id)
        if subscription.offer_id != item.id:
            raise _error(
                "subscription_offer_mismatch",
                "This subscription does not use the selected catalogue offer.",
            )
        choices = future_period_choices(
            db,
            FutureInvoicePeriodsQuery(
                account.id,
                subscription.id,
                as_of,
                horizon=12,
                invoice_id=query.invoice_id,
            ),
        )
        if _is_capped(db, subscription):
            if not choices[0].available:
                raise _error(
                    "period_reserved",
                    choices[0].reason or "Quota renewal is already reserved.",
                )
            if query.period_start is not None:
                raise _error(
                    "capped_dates_unavailable",
                    "Quota renewal dates are resolved only after verified settlement.",
                )
            kind = FutureInvoicePeriodKind.capped_pending_renewal
            allowance = (
                subscription.offer_version.usage_allowance
                if subscription.offer_version
                and subscription.offer_version.usage_allowance_id
                else subscription.offer.usage_allowance
            )
            if (
                allowance is None
                or allowance.validity_days is None
                or allowance.validity_days <= 0
                or allowance.included_gb is None
                or allowance.included_gb <= 0
            ):
                raise _error(
                    "capped_policy_unavailable",
                    "Quota size and validity days must be configured before renewal.",
                )
            allowance_gb = Decimal(allowance.included_gb)
            validity_days = allowance.validity_days
        else:
            if query.period_start is None:
                raise _error(
                    "service_period_required",
                    "Choose an available future service period.",
                )
            selected = next(
                (
                    entry
                    for entry in choices
                    if entry.starts_at == _aware(query.period_start)
                ),
                None,
            )
            if selected is None:
                raise _error(
                    "service_period_unavailable",
                    "The selected service period is not an owner-derived future period.",
                )
            if not selected.available:
                raise _error(
                    "service_period_unavailable",
                    selected.reason or "This service period cannot be invoiced.",
                )
            starts_at, ends_at = selected.starts_at, selected.ends_at
            kind = FutureInvoicePeriodKind.monthly
        effective_at = starts_at or as_of
        if subscription.billing_mode is BillingMode.prepaid:
            charge = resolve_prepaid_monthly_charge_detail(
                db, subscription, effective_at
            )
            if (
                charge is None
                or charge.billing_cycle is not BillingCycle.monthly
                or charge.currency.upper() != query.currency.upper()
            ):
                raise _error(
                    "contract_price_unavailable",
                    "The subscription does not have one supported monthly contract price.",
                )
            amount, application, rate_id = (
                charge.unit_price,
                charge.tax_application,
                charge.tax_rate_id,
            )
        else:
            charge_preview = preview_postpaid_recurring_charge(
                db, subscription_id=subscription.id, as_of=effective_at
            )
            if charge_preview.currency.upper() != query.currency.upper():
                raise _error(
                    "contract_price_unavailable",
                    "The subscription contract currency differs from this invoice.",
                )
            if starts_at is not None and (
                charge_preview.period_start != starts_at
                or charge_preview.period_end != ends_at
            ):
                raise _error(
                    "service_period_unavailable",
                    "The selected period no longer matches the billing owner's calendar.",
                )
            base = next(
                entry
                for entry in charge_preview.components
                if entry.kind.value == "base_service"
            )
            amount, application, rate_id = (
                base.unit_price,
                base.tax_application,
                base.tax_rate_id,
            )
        resolution = resolve_subscription_tax(db, subscription)
        provenance = {
            "tax_source": resolution.source.value,
            "customer_tax_policy_version": resolution.customer_tax_policy_version,
            "subscription_updated_at": subscription.updated_at.isoformat()
            if subscription.updated_at
            else None,
        }
    else:
        if (
            price.price_type is not PriceType.one_time
            or query.period_start is not None
            or query.subscription_id is not None
        ):
            raise _error(
                "one_time_period_invalid",
                "One-time items use quantity without service-period dates.",
            )
        tax_resolution = resolve_standalone_catalogue_tax(
            db,
            StandaloneCatalogueTaxQuery(
                account.id,
                item.id if query.item_kind is CatalogueItemKind.offer else None,
                application,
            ),
        )
        application, rate_id = (
            TaxApplication(tax_resolution.tax_application),
            tax_resolution.tax_rate_id,
        )
        provenance = {
            "tax_source": tax_resolution.source.value,
            "customer_tax_policy_version": tax_resolution.customer_tax_policy_version,
        }
    if rate_id is not None:
        rate = db.get(TaxRate, rate_id)
        if rate is None or not rate.is_active:
            raise _error(
                "tax_rate_unavailable", "The applicable VAT rate is not active."
            )
        tax_rate = Decimal(rate.rate)
    try:
        from dotmac_billing.service import quote_catalogue_line_amount_v1
        from dotmac_tax.service import quote_legacy_commercial_amounts_v1
    except ImportError as exc:
        raise _error(
            "catalogue_capability_unavailable",
            "Catalogue invoicing requires qualified Starter Billing and Tax package upgrades.",
        ) from exc
    line_amount = quote_catalogue_line_amount_v1(
        Money(amount, Currency(query.currency, 2)), query.quantity
    )
    amounts = quote_legacy_commercial_amounts_v1(
        line_amount, tax_rate, application.value
    )
    gross_or_net, tax, total = (
        amounts.line_amount.amount,
        amounts.tax_amount.amount,
        amounts.invoice_total.amount,
    )
    if total > Decimal("9999999999.99"):
        raise _error(
            "quantity_invalid", "This quantity exceeds the invoice line amount limit."
        )
    description = item.name
    if starts_at is not None and ends_at is not None:
        zone = _monthly_cadence().zone()
        description = f"{item.name} ({starts_at.astimezone(zone).date()} → {ends_at.astimezone(zone).date()})"
    if kind is FutureInvoicePeriodKind.capped_pending_renewal:
        description = f"{item.name} · {allowance_gb} GB / {validity_days} days · validity starts after settlement"
    evidence = {
        "query": asdict(query),
        "description": description,
        "unit_price": str(amount),
        "tax_rate_id": str(rate_id) if rate_id else None,
        "tax_rate": str(tax_rate),
        "tax_application": application.value,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "price_updated_at": price.updated_at.isoformat() if price.updated_at else None,
        "provenance": provenance,
        "allowance_gb": str(allowance_gb) if allowance_gb is not None else None,
        "validity_days": validity_days,
    }
    return FutureInvoiceLinePreview(
        description,
        query.quantity,
        amount,
        rate_id,
        application,
        tax,
        total,
        subscription.id if subscription else None,
        starts_at,
        ends_at,
        kind,
        FutureInvoiceLineSelection(query, _fingerprint(evidence)),
        allowance_gb,
        validity_days,
        str(provenance["tax_source"])
        if provenance.get("tax_source") is not None
        else None,
        int(str(provenance["customer_tax_policy_version"]))
        if isinstance(provenance.get("customer_tax_policy_version"), int)
        else None,
    )


def confirm_future_invoice_line(
    db: Session,
    *,
    account_id: UUID,
    currency: str,
    selection: FutureInvoiceLineSelection,
    invoice_id: UUID | None = None,
) -> FutureInvoiceLinePreview:
    query = selection.query
    now = datetime.now(UTC)
    observed_at = _aware(query.as_of)
    if now - observed_at > timedelta(minutes=15) or observed_at > now + timedelta(
        seconds=5
    ):
        raise _error(
            "stale_catalogue_preview",
            "The catalogue preview expired. Review the lines again.",
        )
    if (
        query.account_id != account_id
        or query.currency.upper() != currency.upper()
        or query.invoice_id != invoice_id
    ):
        raise _error(
            "catalogue_preview_mismatch",
            "The catalogue preview does not belong to this invoice.",
        )
    preview = preview_future_invoice_line(db, query)
    if preview.starts_at is not None and preview.starts_at < now:
        raise _error(
            "service_period_no_longer_future",
            "This service period has started. Review the future periods again.",
        )
    if preview.selection.fingerprint != selection.fingerprint:
        raise _error(
            "stale_catalogue_preview",
            "Price, VAT or service dates changed. Review the lines again.",
        )
    return preview


def preview_line_metadata(preview: FutureInvoiceLinePreview) -> dict[str, object]:
    query = preview.selection.query
    result: dict[str, object] = {
        "future_catalogue_invoice": True,
        "catalogue_item_kind": query.item_kind.value,
        "catalogue_item_id": str(query.item_id),
        "catalogue_price_id": str(query.price_id),
        "preview_fingerprint": preview.selection.fingerprint,
        "kind": "base_subscription"
        if preview.subscription_id
        else "catalogue_one_time",
        "future_invoice": {
            "period_kind": preview.period_kind.value,
            "selection": selection_to_wire(preview.selection),
            "allowance_gb": str(preview.allowance_gb)
            if preview.allowance_gb is not None
            else None,
            "validity_days": preview.validity_days,
            "tax_source": preview.tax_source,
            "customer_tax_policy_version": preview.customer_tax_policy_version,
        },
    }
    if preview.starts_at is not None and preview.ends_at is not None:
        result["billing_period_start"] = preview.starts_at.isoformat()
        result["billing_period_end"] = preview.ends_at.isoformat()
    return result


def selection_to_wire(selection: FutureInvoiceLineSelection) -> dict[str, object]:
    query = selection.query
    return {
        "fingerprint": selection.fingerprint,
        "query": {
            "account_id": str(query.account_id),
            "currency": query.currency,
            "item_kind": query.item_kind.value,
            "item_id": str(query.item_id),
            "price_id": str(query.price_id),
            "as_of": query.as_of.isoformat(),
            "quantity": str(query.quantity),
            "subscription_id": str(query.subscription_id)
            if query.subscription_id
            else None,
            "period_start": query.period_start.isoformat()
            if query.period_start
            else None,
            "invoice_id": str(query.invoice_id) if query.invoice_id else None,
        },
    }


def selection_from_wire(value: object) -> FutureInvoiceLineSelection:
    if not isinstance(value, dict) or not isinstance(value.get("query"), dict):
        raise _error(
            "invalid_catalogue_selection",
            "Review the catalogue selection before saving.",
        )
    query = value["query"]
    try:
        return FutureInvoiceLineSelection(
            PreviewFutureInvoiceLineQuery(
                account_id=UUID(str(query["account_id"])),
                currency=str(query["currency"]),
                item_kind=CatalogueItemKind(str(query["item_kind"])),
                item_id=UUID(str(query["item_id"])),
                price_id=UUID(str(query["price_id"])),
                as_of=datetime.fromisoformat(str(query["as_of"])),
                quantity=Decimal(str(query.get("quantity", "1"))),
                subscription_id=UUID(str(query["subscription_id"]))
                if query.get("subscription_id")
                else None,
                period_start=datetime.fromisoformat(str(query["period_start"]))
                if query.get("period_start")
                else None,
                invoice_id=UUID(str(query["invoice_id"]))
                if query.get("invoice_id")
                else None,
            ),
            str(value["fingerprint"]),
        )
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise _error(
            "invalid_catalogue_selection",
            "Catalogue selection is malformed. Review the line again.",
        ) from exc


def validate_catalogue_draft_for_issue(db: Session, invoice: Invoice) -> None:
    """Revalidate persisted catalogue evidence without expiring a saved document.

    The issuer owns the transaction/locks. A changed price, date authority or
    policy requires a new reviewed draft; this reader never reprices it.
    """
    from dataclasses import replace

    for line in invoice.lines:
        if (
            not line.is_active
            or not isinstance(line.metadata_, dict)
            or line.metadata_.get("future_catalogue_invoice") is not True
        ):
            continue
        future = line.metadata_.get("future_invoice")
        if not isinstance(future, dict):
            raise _error(
                "invalid_catalogue_selection",
                "Catalogue source evidence is missing. Review the draft again.",
            )
        selection = selection_from_wire(future.get("selection"))
        if (
            selection.query.account_id != invoice.account_id
            or selection.query.currency.upper() != invoice.currency.upper()
        ):
            raise _error(
                "catalogue_preview_mismatch",
                "Catalogue evidence belongs to a different account or currency.",
            )
        preview = preview_future_invoice_line(
            db, replace(selection.query, as_of=datetime.now(UTC), invoice_id=invoice.id)
        )
        if (
            preview.description != line.description
            or preview.quantity != line.quantity
            or preview.unit_price != line.unit_price
            or preview.tax_rate_id != line.tax_rate_id
            or preview.tax_application is not line.tax_application
            or preview.subscription_id != line.subscription_id
            or preview.period_kind.value != future.get("period_kind")
        ):
            raise _error(
                "stale_catalogue_preview",
                "Catalogue price, VAT or service evidence changed. Review the draft before issuing.",
            )
        if preview.tax_source != future.get(
            "tax_source"
        ) or preview.customer_tax_policy_version != future.get(
            "customer_tax_policy_version"
        ):
            raise _error(
                "stale_catalogue_preview",
                "Customer VAT policy changed. Review the draft before issuing.",
            )
        if line.tax_rate_id is not None:
            from dotmac_tax.service import quote_legacy_commercial_amounts_v1

            snapshot_rate = line.tax_rate_percent_snapshot
            if (
                snapshot_rate is None
                or quote_legacy_commercial_amounts_v1(
                    Money(line.amount, Currency(invoice.currency, 2)),
                    snapshot_rate,
                    line.tax_application.value,
                ).tax_amount.amount
                != preview.tax_total
            ):
                raise _error(
                    "stale_catalogue_preview",
                    "VAT rate changed. Review the draft before issuing.",
                )
        if preview.starts_at is not None and (
            preview.starts_at.isoformat() != line.metadata_.get("billing_period_start")
            or preview.ends_at is None
            or preview.ends_at.isoformat() != line.metadata_.get("billing_period_end")
        ):
            raise _error(
                "service_period_unavailable",
                "Service dates changed. Review the draft before issuing.",
            )
        if preview.period_kind is FutureInvoicePeriodKind.capped_pending_renewal and (
            str(preview.allowance_gb) != future.get("allowance_gb")
            or preview.validity_days != future.get("validity_days")
        ):
            raise _error(
                "capped_policy_unavailable",
                "Quota size or validity changed. Review the draft before issuing.",
            )
