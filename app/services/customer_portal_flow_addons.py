"""Customer self-service add-on purchase with exact adjustment evidence.

Add-ons available to a subscription come from its offer's ``OfferAddOn`` links.
A paid purchase consumes prepaid account funding through
``financial.account_adjustments``. The add-on owner previews the price,
subscription state, funding, receivables, and exact ledger consequence; locked
confirmation rejects stale previews and links the resulting entitlement to the
exact adjustment. Ownership is enforced against the caller's account.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import LedgerCategory
from app.models.catalog import (
    AddOn,
    OfferAddOn,
    PriceType,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.idempotency import IdempotencyKey
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import (
    AccountAdjustmentConfirm,
    AccountAdjustmentPreviewRequest,
)
from app.services import catalog as catalog_service
from app.services.audit import AuditEvents
from app.services.billing._common import get_account_credit_balance, lock_account
from app.services.billing.adjustments import (
    AccountAdjustmentPreview,
    AccountAdjustments,
)
from app.services.common import coerce_uuid, round_money, to_decimal
from app.services.customer_context import optional_customer_account_id
from app.services.customer_financial_position import get_customer_financial_position


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
    account_id = optional_customer_account_id(db, customer)
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


@dataclass(frozen=True)
class AddonPurchasePreview:
    subscription: object
    add_on: AddOn
    quantity: int
    unit_amount: Decimal
    charge: Decimal
    currency: str
    subscription_status: str
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    postpaid_receivables: Decimal
    collection_blocking_balance: Decimal
    shortfall: Decimal
    allowed: bool
    rejection_reason: str | None
    adjustment_preview: AccountAdjustmentPreview | None
    fingerprint: str

    def as_dict(self) -> dict[str, object]:
        return {
            "add_on_id": str(self.add_on.id),
            "quantity": self.quantity,
            "unit_amount": self.unit_amount,
            "charge": self.charge,
            "currency": self.currency,
            "subscription_status": self.subscription_status,
            "prepaid_funding_before": self.prepaid_funding_before,
            "prepaid_funding_after": self.prepaid_funding_after,
            "postpaid_receivables": self.postpaid_receivables,
            "collection_blocking_balance": self.collection_blocking_balance,
            "shortfall": self.shortfall,
            "can_afford": self.shortfall == Decimal("0.00"),
            "allowed": self.allowed,
            "rejection_reason": self.rejection_reason,
            "ledger_entry_type": (
                self.adjustment_preview.ledger_entry_type
                if self.adjustment_preview
                else None
            ),
            "ledger_source": (
                self.adjustment_preview.ledger_source
                if self.adjustment_preview
                else None
            ),
            "ledger_amount": (
                self.adjustment_preview.ledger_amount
                if self.adjustment_preview
                else Decimal("0.00")
            ),
            "access_consequence": "none_addon_purchase_only",
            "preview_fingerprint": self.fingerprint,
        }


def _purchase_fingerprint(**values: object) -> str:
    normalized = {
        key: f"{value:.2f}" if isinstance(value, Decimal) else str(value)
        for key, value in values.items()
    }
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_purchase_preview(
    db: Session,
    subscription,
    add_on_id: str,
    quantity: int,
) -> AddonPurchasePreview:
    add_on, unit_amount, currency = _resolve_purchasable(
        db, subscription, add_on_id, quantity
    )
    charge = round_money(unit_amount * quantity)
    account_id = str(subscription.subscriber_id)
    status_value = str(getattr(subscription.status, "value", subscription.status or ""))
    origin_ref = f"{subscription.id}:{add_on.id}:{quantity}"
    adjustment_preview = None
    if charge > Decimal("0.00"):
        adjustment_preview = AccountAdjustments.preview(
            db,
            AccountAdjustmentPreviewRequest(
                account_id=subscription.subscriber_id,
                category=LedgerCategory.custom_service,
                amount=charge,
                currency=currency,
                memo=f"Add-on purchase: {add_on.name}"
                + (f" x{quantity}" if quantity > 1 else ""),
                reason="Customer-confirmed add-on purchase",
            ),
            origin="addon_purchase",
            origin_ref=origin_ref,
        )
        funding_before = adjustment_preview.prepaid_funding_before
        funding_after = adjustment_preview.prepaid_funding_after
        receivables = adjustment_preview.postpaid_receivables
        blocking = adjustment_preview.collection_blocking_balance
        shortfall = adjustment_preview.shortfall
    else:
        position = get_customer_financial_position(db, subscription.subscriber_id)
        funding_before = round_money(
            get_account_credit_balance(db, account_id, currency=currency)
        )
        funding_after = funding_before
        receivables = round_money(position.open_invoice_balance)
        blocking = round_money(position.collection_blocking_balance)
        shortfall = Decimal("0.00")

    active = subscription.status == SubscriptionStatus.active
    affordable = shortfall == Decimal("0.00")
    allowed = active and affordable
    rejection_reason = None
    if not active:
        rejection_reason = "subscription_not_active"
    elif not affordable:
        rejection_reason = "insufficient_prepaid_funding"
    fingerprint = _purchase_fingerprint(
        kind="addon_purchase",
        subscription_id=subscription.id,
        subscription_status=status_value,
        offer_id=subscription.offer_id,
        add_on_id=add_on.id,
        quantity=quantity,
        unit_amount=unit_amount,
        charge=charge,
        currency=currency,
        prepaid_funding_before=funding_before,
        prepaid_funding_after=funding_after,
        postpaid_receivables=receivables,
        collection_blocking_balance=blocking,
        adjustment_fingerprint=(
            adjustment_preview.fingerprint if adjustment_preview else "no-ledger-entry"
        ),
        allowed=allowed,
    )
    return AddonPurchasePreview(
        subscription=subscription,
        add_on=add_on,
        quantity=quantity,
        unit_amount=unit_amount,
        charge=charge,
        currency=currency,
        subscription_status=status_value,
        prepaid_funding_before=funding_before,
        prepaid_funding_after=funding_after,
        postpaid_receivables=receivables,
        collection_blocking_balance=blocking,
        shortfall=shortfall,
        allowed=allowed,
        rejection_reason=rejection_reason,
        adjustment_preview=adjustment_preview,
        fingerprint=fingerprint,
    )


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

    return {
        "available": options,
        "active": active,
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
    """Owner preview for one add-on purchase and its exact financial result."""
    subscription = _owned_subscription(db, customer, subscription_id)
    if subscription is None:
        return None
    return _build_purchase_preview(db, subscription, add_on_id, quantity).as_dict()


_IDEMPOTENCY_SCOPE = "addon_purchase"


def _find_key(db: Session, key: str) -> IdempotencyKey | None:
    return db.scalars(
        select(IdempotencyKey).where(
            IdempotencyKey.scope == _IDEMPOTENCY_SCOPE,
            IdempotencyKey.key == key,
        )
    ).first()


def _replay_addon_result(
    db: Session,
    ref_id: str | None,
    *,
    preview_fingerprint: str,
) -> dict:
    sub_add_on = db.get(SubscriptionAddOn, coerce_uuid(ref_id)) if ref_id else None
    if sub_add_on is None:
        raise HTTPException(
            status_code=409, detail="Add-on idempotency record has no purchase"
        )
    if sub_add_on.purchase_preview_fingerprint != preview_fingerprint:
        raise HTTPException(
            status_code=409,
            detail="Idempotency key was used for another add-on preview",
        )
    adjustment = sub_add_on.account_adjustment
    return {
        "success": True,
        "replayed": True,
        "subscription_add_on_id": ref_id,
        "quantity": int(getattr(sub_add_on, "quantity", 1) or 1),
        "charge": round_money(adjustment.amount) if adjustment else Decimal("0.00"),
        "currency": adjustment.currency if adjustment else "NGN",
        "prepaid_funding_before": (
            round_money(adjustment.prepaid_funding_before) if adjustment else None
        ),
        "prepaid_funding_after": (
            round_money(adjustment.prepaid_funding_after) if adjustment else None
        ),
        "postpaid_receivables": (
            round_money(adjustment.postpaid_receivables) if adjustment else None
        ),
        "collection_blocking_balance": (
            round_money(adjustment.collection_blocking_balance) if adjustment else None
        ),
        "account_adjustment_id": str(adjustment.id) if adjustment else None,
        "ledger_entry_id": str(adjustment.ledger_entry_id) if adjustment else None,
        "preview_fingerprint": sub_add_on.purchase_preview_fingerprint,
        "access_consequence": "none_addon_purchase_only",
    }


def purchase_addon(
    db: Session,
    customer: dict,
    subscription_id: str,
    add_on_id: str,
    quantity: int = 1,
    *,
    preview_fingerprint: str,
    idempotency_key: str,
) -> dict:
    """Confirm the exact previewed add-on and adjustment atomically."""
    subscription = _owned_subscription(db, customer, subscription_id)
    if subscription is None:
        raise ValueError("Service not found")
    if len(idempotency_key.strip()) < 16:
        raise ValueError("A stable idempotency key is required")
    if len(preview_fingerprint.strip()) != 64:
        raise ValueError("A valid add-on preview fingerprint is required")

    account_id = str(subscription.subscriber_id)
    prior = _find_key(db, idempotency_key)
    if prior is not None:
        if str(prior.account_id) != account_id:
            raise ValueError("Idempotency key already used")
        return _replay_addon_result(
            db, prior.ref_id, preview_fingerprint=preview_fingerprint
        )

    # Serialize the entitlement and funding decision with every other account
    # debit, then recompute the exact preview under that lock.
    lock_account(db, str(subscription.subscriber_id))
    db.refresh(subscription)
    prior = _find_key(db, idempotency_key)
    if prior is not None:
        if str(prior.account_id) != account_id:
            raise ValueError("Idempotency key already used")
        return _replay_addon_result(
            db, prior.ref_id, preview_fingerprint=preview_fingerprint
        )

    preview = _build_purchase_preview(db, subscription, add_on_id, quantity)
    if preview.fingerprint != preview_fingerprint:
        raise HTTPException(
            status_code=409,
            detail="Add-on price, service, or funding changed; preview again",
        )
    if not preview.allowed:
        return {
            "success": False,
            "reason": preview.rejection_reason,
            "subscription_status": preview.subscription_status,
            "charge": preview.charge,
            "prepaid_funding_before": preview.prepaid_funding_before,
            "prepaid_funding_after": preview.prepaid_funding_after,
            "postpaid_receivables": preview.postpaid_receivables,
            "collection_blocking_balance": preview.collection_blocking_balance,
            "shortfall": preview.shortfall,
            "currency": preview.currency,
            "preview_fingerprint": preview.fingerprint,
        }

    sub_add_on = SubscriptionAddOn(
        subscription_id=subscription.id,
        add_on_id=coerce_uuid(str(preview.add_on.id)),
        quantity=quantity,
        start_at=datetime.now(UTC),
        purchase_preview_fingerprint=preview.fingerprint,
        purchase_idempotency_key=idempotency_key,
    )
    db.add(sub_add_on)
    db.flush()

    adjustment_result = None
    if preview.adjustment_preview is not None:
        adjustment_preview = preview.adjustment_preview
        adjustment_result = AccountAdjustments.confirm(
            db,
            AccountAdjustmentConfirm(
                account_id=adjustment_preview.account_id,
                category=adjustment_preview.category,
                amount=adjustment_preview.amount,
                currency=adjustment_preview.currency,
                memo=adjustment_preview.memo,
                reason=adjustment_preview.reason,
                preview_fingerprint=adjustment_preview.fingerprint,
                idempotency_key=idempotency_key,
            ),
            origin="addon_purchase",
            origin_ref=f"{subscription.id}:{preview.add_on.id}:{quantity}",
            actor_type=AuditActorType.user,
            actor_id=account_id,
            commit=False,
        )
        sub_add_on.account_adjustment_id = adjustment_result.adjustment.id

    # Data top-up: stamp its validity window and credit the purchased GB to the
    # current period's quota bucket.
    if preview.add_on.grant_gb:
        from app.services.usage import grant_data_topup

        grant_data_topup(db, subscription, sub_add_on, preview.add_on)

    db.add(
        IdempotencyKey(
            scope=_IDEMPOTENCY_SCOPE,
            key=idempotency_key,
            account_id=subscription.subscriber_id,
            ref_id=str(sub_add_on.id),
        )
    )
    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=account_id,
            action="confirm",
            entity_type="subscription_add_on_purchase",
            entity_id=str(sub_add_on.id),
            metadata_={
                "subscription_id": str(subscription.id),
                "add_on_id": str(preview.add_on.id),
                "quantity": quantity,
                "charge": str(preview.charge),
                "currency": preview.currency,
                "prepaid_funding_before": str(preview.prepaid_funding_before),
                "prepaid_funding_after": str(preview.prepaid_funding_after),
                "postpaid_receivables": str(preview.postpaid_receivables),
                "preview_fingerprint": preview.fingerprint,
                "account_adjustment_id": (
                    str(adjustment_result.adjustment.id)
                    if adjustment_result is not None
                    else None
                ),
                "ledger_entry_id": (
                    str(adjustment_result.ledger_entry.id)
                    if adjustment_result is not None
                    else None
                ),
                "access_consequence": "none_addon_purchase_only",
            },
        ),
    )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Only a same-key race is a replay: re-fetch the key and, if a
        # concurrent request committed it, return that result. Any other
        # integrity failure (FK, constraint) is a real error — re-raise it
        # rather than reporting a phantom success.
        prior = _find_key(db, idempotency_key)
        if prior is not None and prior.ref_id:
            return _replay_addon_result(
                db, prior.ref_id, preview_fingerprint=preview_fingerprint
            )
        raise
    db.refresh(sub_add_on)
    return {
        "success": True,
        "subscription_add_on_id": str(sub_add_on.id),
        "add_on_name": preview.add_on.name,
        "quantity": quantity,
        "charge": preview.charge,
        "currency": preview.currency,
        "prepaid_funding_before": preview.prepaid_funding_before,
        "prepaid_funding_after": preview.prepaid_funding_after,
        "postpaid_receivables": preview.postpaid_receivables,
        "collection_blocking_balance": preview.collection_blocking_balance,
        "account_adjustment_id": (
            str(adjustment_result.adjustment.id)
            if adjustment_result is not None
            else None
        ),
        "ledger_entry_id": (
            str(adjustment_result.ledger_entry.id)
            if adjustment_result is not None
            else None
        ),
        "preview_fingerprint": preview.fingerprint,
        "access_consequence": "none_addon_purchase_only",
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
