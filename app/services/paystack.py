"""Paystack payment gateway integration service."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import uuid
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

PAYSTACK_API_BASE = "https://api.paystack.co"


def _get_secret_key(db: Session | None = None) -> str:
    """Resolve the Paystack secret key from settings or env."""
    if db:
        val = resolve_value(db, SettingDomain.billing, "paystack_secret_key")
        if val:
            return str(val)
    return os.getenv("PAYSTACK_SECRET_KEY", "")


def _get_public_key(db: Session | None = None) -> str:
    """Resolve the Paystack public key from settings or env."""
    if db:
        val = resolve_value(db, SettingDomain.billing, "paystack_public_key")
        if val:
            return str(val)
    return os.getenv("PAYSTACK_PUBLIC_KEY", "")


def amount_to_kobo(amount: Decimal | float | int) -> int:
    """Convert a naira amount to kobo (NGN × 100)."""
    return int(Decimal(str(amount)) * 100)


def kobo_to_naira(kobo: int) -> Decimal:
    """Convert kobo back to naira."""
    return Decimal(kobo) / 100


def generate_reference(invoice_number: str | None = None) -> str:
    """Generate a unique payment reference.

    Format: DMAC-{invoice_number}-{short_uuid} or DMAC-{short_uuid}
    """
    short = uuid.uuid4().hex[:8]
    if invoice_number:
        return f"DMAC-{invoice_number}-{short}"
    return f"DMAC-{short}"


def initialize_transaction(
    db: Session,
    *,
    email: str,
    amount_kobo: int,
    reference: str,
    callback_url: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Initialize a Paystack transaction.

    Args:
        db: Database session for settings resolution.
        email: Customer email address.
        amount_kobo: Amount in kobo (NGN × 100).
        reference: Unique transaction reference.
        callback_url: URL Paystack redirects to after payment.
        metadata: Optional metadata dict attached to the transaction.

    Returns:
        Dict with ``authorization_url``, ``access_code``, ``reference``.

    Raises:
        httpx.HTTPStatusError: On non-2xx response from Paystack.
        ValueError: If secret key is not configured.
    """
    secret_key = _get_secret_key(db)
    if not secret_key:
        raise ValueError("Paystack secret key is not configured")

    payload: dict[str, Any] = {
        "email": email,
        "amount": amount_kobo,
        "reference": reference,
        "callback_url": callback_url,
    }
    if metadata:
        payload["metadata"] = metadata

    resp = httpx.post(
        f"{PAYSTACK_API_BASE}/transaction/initialize",
        json=payload,
        headers={"Authorization": f"Bearer {secret_key}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if not data.get("status"):
        logger.error("Paystack initialize failed: %s", data.get("message"))
        raise ValueError(data.get("message", "Paystack initialization failed"))

    return data["data"]


def verify_transaction(db: Session, reference: str) -> dict[str, Any]:
    """Verify a Paystack transaction by reference.

    Args:
        db: Database session for settings resolution.
        reference: The transaction reference to verify.

    Returns:
        Dict with transaction status and data from Paystack.

    Raises:
        httpx.HTTPStatusError: On non-2xx response.
        ValueError: If secret key is not configured.
    """
    secret_key = _get_secret_key(db)
    if not secret_key:
        raise ValueError("Paystack secret key is not configured")

    resp = httpx.get(
        f"{PAYSTACK_API_BASE}/transaction/verify/{reference}",
        headers={"Authorization": f"Bearer {secret_key}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if not data.get("status"):
        logger.error("Paystack verify failed: %s", data.get("message"))
        raise ValueError(data.get("message", "Paystack verification failed"))

    return data["data"]


def verify_webhook_signature(
    body: bytes, signature: str, db: Session | None = None
) -> bool:
    """Verify Paystack webhook HMAC-SHA512 signature.

    Args:
        body: Raw request body bytes.
        signature: Value of the X-Paystack-Signature header.
        db: Optional database session for settings resolution.

    Returns:
        True if the signature is valid.
    """
    secret_key = _get_secret_key(db)
    if not secret_key:
        return False

    expected = hmac.new(
        secret_key.encode(),
        body,
        hashlib.sha512,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


def get_public_key(db: Session | None = None) -> str:
    """Return the Paystack public key for frontend use."""
    return _get_public_key(db)
