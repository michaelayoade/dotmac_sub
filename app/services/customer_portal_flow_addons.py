"""Customer self-service add-on purchase, paid from the wallet balance.

Add-ons available to a subscription come from its offer's ``OfferAddOn`` links.
A purchase is charged from the customer's wallet credit balance via a ledger
debit — mirroring the prepaid plan-change charge (``_create_prepaid_plan_change_debit``)
— so we never invent invoice logic. Insufficient balance is rejected (the
customer tops up first). Ownership is enforced against the caller's account.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.billing import (
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import (
    AddOn,
    OfferAddOn,
    PriceType,
    SubscriptionAddOn,
)
from app.models.idempotency import IdempotencyKey
from app.services import catalog as catalog_service
from app.services.billing._common import get_account_credit_balance, lock_account
from app.services.common import coerce_uuid, round_money, to_decimal


def _addon_active_price(add_on: AddOn) -> tuple[Decimal, str]:
    """Best price for an add-on: prefer a recurring active price, else any
    active price. Returns (amount, currency); (0, NGN) when unpriced."""
    prices = [p for p in (add_on.prices or []) if p.is_active]
    if not prices:
        return Decimal("0.00"), "NGN"
    chosen = next((p for p in prices if p.price_type == PriceType.recurring), prices[0])
    return round_money(to_decimal(chosen.amount or 0)), str(chosen.currency or "NGN")


def _owned_subscription(db: Session, customer: dict, subscription_id: str):
    """Return the subscription iff it belongs to the caller, else None."""
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if not subscription:
        return None
    account_id = customer.get("account_id")
    if not account_id or str(subscription.subscriber_id) != str(account_id):
        return None
    return subscription


def _offer_links(db: Session, offer_id) -> list[tuple[OfferAddOn, AddOn]]:
    """Active add-ons offered for a subscription's offer, with their link row."""
    if not offer_id:
        return []
    rows = (
        db.query(OfferAddOn, AddOn)
        .join(AddOn, AddOn.id == OfferAddOn.add_on_id)
        .filter(OfferAddOn.offer_id == offer_id)
        .filter(AddOn.is_active.is_(True))
        .all()
    )
    return [(link, add_on) for link, add_on in rows]


def _serialize_option(link: OfferAddOn, add_on: AddOn) -> dict:
    amount, currency = _addon_active_price(add_on)
    return {
        "add_on_id": str(add_on.id),
        "name": add_on.name,
        "addon_type": getattr(add_on.addon_type, "value", str(add_on.addon_type)),
        "description": add_on.description,
        "amount": float(amount),
        "currency": currency,
        "min_quantity": int(link.min_quantity or 1),
        "max_quantity": link.max_quantity,
        "is_required": bool(link.is_required),
        # Data top-up: GB granted to the quota bucket on purchase (null otherwise).
        "grant_gb": add_on.grant_gb,
    }


def list_available_addons(
    db: Session, customer: dict, subscription_id: str
) -> dict | None:
    """Add-ons the customer can buy for this service, plus active ones."""
    subscription = _owned_subscription(db, customer, subscription_id)
    if subscription is None:
        return None

    options = [
        _serialize_option(link, add_on)
        for link, add_on in _offer_links(db, subscription.offer_id)
    ]

    active_rows = (
        db.query(SubscriptionAddOn, AddOn)
        .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
        .filter(SubscriptionAddOn.subscription_id == subscription.id)
        .all()
    )
    now = datetime.now(UTC)

    def _is_expired(end_at) -> bool:
        if end_at is None:
            return False
        if end_at.tzinfo is None:
            end_at = end_at.replace(tzinfo=UTC)
        return end_at < now

    active = [
        {
            "id": str(sa.id),
            "add_on_id": str(sa.add_on_id),
            "name": add_on.name,
            "quantity": int(sa.quantity or 1),
            "addon_type": getattr(add_on.addon_type, "value", str(add_on.addon_type)),
            # Data bundles: GB granted per unit (null for non-data add-ons).
            "grant_gb": add_on.grant_gb,
            "total_grant_gb": (
                add_on.grant_gb * int(sa.quantity or 1)
                if add_on.grant_gb is not None
                else None
            ),
            "starts_at": sa.start_at,
            # Null = lasts until the end of the billing period it was bought in.
            "expires_at": sa.end_at,
            "validity_days": add_on.validity_days,
            "is_expired": _is_expired(sa.end_at),
        }
        for sa, add_on in active_rows
    ]

    balance = get_account_credit_balance(db, str(subscription.subscriber_id))
    return {
        "available": options,
        "active": active,
        "wallet_balance": round_money(balance),
        "currency": "NGN",
    }


def _resolve_purchasable(
    db: Session, subscription, add_on_id: str, quantity: int
) -> tuple[AddOn, Decimal, str]:
    """Validate the add-on is offered for this subscription and the quantity is
    in range; return (add_on, unit_amount, currency). Raises ValueError."""
    links = _offer_links(db, subscription.offer_id)
    match = next(
        ((link, ao) for link, ao in links if str(ao.id) == str(add_on_id)), None
    )
    if match is None:
        raise ValueError("Add-on is not available for this service")
    link, add_on = match
    min_q = int(link.min_quantity or 1)
    if quantity < min_q:
        raise ValueError(f"Minimum quantity is {min_q}")
    if link.max_quantity is not None and quantity > int(link.max_quantity):
        raise ValueError(f"Maximum quantity is {link.max_quantity}")
    amount, currency = _addon_active_price(add_on)
    return add_on, amount, currency


def get_addon_quote(
    db: Session,
    customer: dict,
    subscription_id: str,
    add_on_id: str,
    quantity: int = 1,
) -> dict | None:
    """Cost of buying ``quantity`` of an add-on, vs the wallet balance."""
    subscription = _owned_subscription(db, customer, subscription_id)
    if subscription is None:
        return None
    _add_on, unit_amount, currency = _resolve_purchasable(
        db, subscription, add_on_id, quantity
    )
    charge = round_money(unit_amount * quantity)
    balance = round_money(
        get_account_credit_balance(db, str(subscription.subscriber_id))
    )
    shortfall = charge - balance
    shortfall = shortfall if shortfall > Decimal("0.00") else Decimal("0.00")
    return {
        "add_on_id": str(add_on_id),
        "quantity": quantity,
        "unit_amount": unit_amount,
        "charge": charge,
        "currency": currency,
        "current_balance": balance,
        "shortfall": shortfall,
        "can_afford": shortfall <= Decimal("0.00"),
    }


_IDEMPOTENCY_SCOPE = "addon_purchase"


def _find_key(db: Session, key: str) -> IdempotencyKey | None:
    return db.scalars(
        select(IdempotencyKey).where(
            IdempotencyKey.scope == _IDEMPOTENCY_SCOPE,
            IdempotencyKey.key == key,
        )
    ).first()


def _replay_addon_result(db: Session, ref_id: str | None) -> dict:
    sub_add_on = db.get(SubscriptionAddOn, coerce_uuid(ref_id)) if ref_id else None
    return {
        "success": True,
        "replayed": True,
        "subscription_add_on_id": ref_id,
        "quantity": int(getattr(sub_add_on, "quantity", 1) or 1),
    }


def purchase_addon(
    db: Session,
    customer: dict,
    subscription_id: str,
    add_on_id: str,
    quantity: int = 1,
    idempotency_key: str | None = None,
) -> dict:
    """Buy an add-on, charged from the wallet balance. Rejects (without any
    write) when the balance is insufficient. Idempotent on ``idempotency_key``:
    a replay returns the original purchase instead of charging again."""
    subscription = _owned_subscription(db, customer, subscription_id)
    if subscription is None:
        raise ValueError("Service not found")

    account_id = str(subscription.subscriber_id)
    if idempotency_key:
        prior = _find_key(db, idempotency_key)
        if prior is not None:
            if str(prior.account_id) != account_id:
                raise ValueError("Idempotency key already used")
            return _replay_addon_result(db, prior.ref_id)

    # Serialize the wallet read-modify-write against concurrent add-on/autopay/
    # plan-change debits so two writers can't both read the same balance and
    # overspend it.
    lock_account(db, str(subscription.subscriber_id))

    add_on, unit_amount, currency = _resolve_purchasable(
        db, subscription, add_on_id, quantity
    )
    charge = round_money(unit_amount * quantity)
    balance = round_money(
        get_account_credit_balance(db, str(subscription.subscriber_id))
    )

    if charge > Decimal("0.00") and charge > balance:
        shortfall = round_money(charge - balance)
        return {
            "success": False,
            "reason": "insufficient_balance",
            "charge": charge,
            "current_balance": balance,
            "shortfall": shortfall,
            "currency": currency,
        }

    sub_add_on = SubscriptionAddOn(
        subscription_id=subscription.id,
        add_on_id=coerce_uuid(str(add_on.id)),
        quantity=quantity,
        start_at=datetime.now(UTC),
    )
    db.add(sub_add_on)

    if charge > Decimal("0.00"):
        db.add(
            LedgerEntry(
                account_id=subscription.subscriber_id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                category=LedgerCategory.custom_service,
                amount=charge,
                currency=currency,
                memo=f"Add-on purchase: {add_on.name}"
                + (f" x{quantity}" if quantity > 1 else ""),
            )
        )

    # Data top-up: stamp its validity window and credit the purchased GB to the
    # current period's quota bucket.
    if add_on.grant_gb:
        from app.services.usage import grant_data_topup

        grant_data_topup(db, subscription, sub_add_on, add_on)

    if idempotency_key:
        db.flush()  # assign sub_add_on.id before referencing it
        db.add(
            IdempotencyKey(
                scope=_IDEMPOTENCY_SCOPE,
                key=idempotency_key,
                account_id=subscription.subscriber_id,
                ref_id=str(sub_add_on.id),
            )
        )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Only a same-key race is a replay: re-fetch the key and, if a
        # concurrent request committed it, return that result. Any other
        # integrity failure (FK, constraint) is a real error — re-raise it
        # rather than reporting a phantom success.
        prior = _find_key(db, idempotency_key) if idempotency_key else None
        if prior is not None and prior.ref_id:
            return _replay_addon_result(db, prior.ref_id)
        raise
    db.refresh(sub_add_on)
    new_balance = round_money(
        get_account_credit_balance(db, str(subscription.subscriber_id))
    )
    return {
        "success": True,
        "subscription_add_on_id": str(sub_add_on.id),
        "add_on_name": add_on.name,
        "quantity": quantity,
        "charge": charge,
        "currency": currency,
        "new_balance": new_balance,
    }


def cancel_addon(
    db: Session, customer: dict, subscription_id: str, sub_add_on_id: str
) -> bool:
    """End one of the caller's add-ons (stops recurring billing from the next
    cycle). Returns False if the add-on isn't found on the caller's service or is
    already ended. No refund is issued — the customer keeps it for the cycle
    already billed."""
    subscription = _owned_subscription(db, customer, subscription_id)
    if subscription is None:
        return False
    sub_add_on = db.get(SubscriptionAddOn, coerce_uuid(sub_add_on_id))
    if (
        sub_add_on is None
        or str(sub_add_on.subscription_id) != str(subscription.id)
        or sub_add_on.end_at is not None
    ):
        return False
    sub_add_on.end_at = datetime.now(UTC)
    db.commit()
    return True
