"""Customer self-service saved cards.

Saved cards are created by capturing the reusable ``authorization`` Paystack
returns on a successful card charge (``payment_method_from_authorization``), and
managed self-scoped: a customer may list / set-default / remove only their own.
The reusable token is never returned to the client.
"""

from __future__ import annotations

import builtins
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import PaymentMethod, PaymentMethodType
from app.schemas.billing import PaymentMethodCreate, PaymentMethodUpdate
from app.services import billing as billing_service
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


def _int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def card_label(authorization: dict) -> str:
    brand = authorization.get("brand") or authorization.get("card_type") or "Card"
    brand = str(brand).strip().title()
    last4 = authorization.get("last4")
    return f"{brand} •••• {last4}" if last4 else brand


def payment_method_from_authorization(
    authorization: dict | None, account_id: str
) -> PaymentMethodCreate | None:
    """Map a Paystack ``authorization`` block to a PaymentMethodCreate, or None
    when it carries no reusable token."""
    if not authorization:
        return None
    code = authorization.get("authorization_code")
    if not code or authorization.get("reusable") is False:
        return None
    return PaymentMethodCreate(
        account_id=coerce_uuid(account_id),
        method_type=PaymentMethodType.card,
        label=card_label(authorization),
        token=code,
        last4=authorization.get("last4"),
        brand=(authorization.get("brand") or authorization.get("card_type")),
        expires_month=_int(authorization.get("exp_month")),
        expires_year=_int(authorization.get("exp_year")),
    )


def save_card_from_authorization(
    db: Session, account_id: str, authorization: dict | None
) -> PaymentMethod | None:
    """Persist a saved card from a Paystack authorization, de-duplicating on the
    card fingerprint. No-op (returns existing/None) when there is nothing
    reusable to save."""
    payload = payment_method_from_authorization(authorization, account_id)
    if payload is None:
        return None
    # De-dup on (last4, brand, expiry) — already on the row, so we avoid
    # fetching + decrypting every stored token on the payment path.
    fingerprint = (
        payload.last4,
        payload.brand,
        payload.expires_month,
        payload.expires_year,
    )
    for existing in list_for_account(db, account_id):
        if (
            existing.last4,
            existing.brand,
            existing.expires_month,
            existing.expires_year,
        ) == fingerprint:
            return existing
    return billing_service.payment_methods.create(db, payload)


def capture_card_after_payment(
    db: Session,
    account_id: str,
    reference: str,
    provider_type: str | None,
) -> PaymentMethod | None:
    """Best-effort: save the card a just-verified Paystack payment used. Only
    Paystack exposes a reusable card authorization here; any failure is swallowed
    so it can never break the payment that already succeeded."""
    if provider_type not in (None, "", "paystack"):
        return None
    try:
        from app.services import paystack

        data = paystack.verify_transaction(db, reference)
        return save_card_from_authorization(db, account_id, data.get("authorization"))
    except Exception:  # noqa: BLE001 - capture is non-critical
        logger.warning("card capture skipped for %s", reference, exc_info=True)
        return None


def list_for_account(db: Session, account_id: str) -> builtins.list[PaymentMethod]:
    return billing_service.payment_methods.list(
        db, account_id, True, "created_at", "desc", 100, 0
    )


def _owned(db: Session, account_id: str, method_id: str) -> PaymentMethod | None:
    method = billing_service.payment_methods.get(db, method_id)
    if not method or str(method.account_id) != str(account_id):
        return None
    return method


def set_default(db: Session, account_id: str, method_id: str) -> PaymentMethod | None:
    if _owned(db, account_id, method_id) is None:
        return None
    return billing_service.payment_methods.update(
        db, method_id, PaymentMethodUpdate(is_default=True)
    )


def remove(db: Session, account_id: str, method_id: str) -> bool:
    if _owned(db, account_id, method_id) is None:
        return False
    billing_service.payment_methods.delete(db, method_id)
    # Don't leave autopay pointing at a card the customer just removed — the
    # token would still be chargeable. Deactivate any mandate on this card.
    from app.models.autopay import AutopayMandate

    for mandate in db.scalars(
        select(AutopayMandate).where(
            AutopayMandate.payment_method_id == coerce_uuid(method_id)
        )
    ).all():
        mandate.is_active = False
    db.commit()
    return True
