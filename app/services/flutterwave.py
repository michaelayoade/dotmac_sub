"""Flutterwave payment gateway integration service."""

from __future__ import annotations

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

FLUTTERWAVE_API_BASE = "https://api.flutterwave.com/v3"


def _get_secret_key(db: Session | None = None) -> str:
    """Resolve the Flutterwave secret key from settings or env."""
    if db:
        val = resolve_value(db, SettingDomain.billing, "flutterwave_secret_key")
        if val:
            return str(val)
    return os.getenv("FLUTTERWAVE_SECRET_KEY", "")


def _get_public_key(db: Session | None = None) -> str:
    """Resolve the Flutterwave public key from settings or env."""
    if db:
        val = resolve_value(db, SettingDomain.billing, "flutterwave_public_key")
        if val:
            return str(val)
    return os.getenv("FLUTTERWAVE_PUBLIC_KEY", "")


def _get_secret_hash(db: Session | None = None) -> str:
    """Resolve the Flutterwave webhook secret hash from settings or env."""
    if db:
        val = resolve_value(db, SettingDomain.billing, "flutterwave_secret_hash")
        if val:
            return str(val)
    return os.getenv("FLUTTERWAVE_SECRET_HASH", "")


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
    amount: Decimal | float | int,
    reference: str,
    redirect_url: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Initialize a Flutterwave payment.

    Args:
        db: Database session for settings resolution.
        email: Customer email address.
        amount: Amount in naira (major currency unit).
        reference: Unique transaction reference.
        redirect_url: URL Flutterwave redirects to after payment.
        metadata: Optional metadata dict attached to the transaction.

    Returns:
        Dict with ``link`` (hosted payment URL) and other data.

    Raises:
        httpx.HTTPStatusError: On non-2xx response from Flutterwave.
        ValueError: If secret key is not configured.
    """
    secret_key = _get_secret_key(db)
    if not secret_key:
        raise ValueError("Flutterwave secret key is not configured")

    payload: dict[str, Any] = {
        "tx_ref": reference,
        "amount": float(Decimal(str(amount))),
        "currency": "NGN",
        "redirect_url": redirect_url,
        "customer": {"email": email},
    }
    if metadata:
        payload["meta"] = metadata

    resp = httpx.post(
        f"{FLUTTERWAVE_API_BASE}/payments",
        json=payload,
        headers={"Authorization": f"Bearer {secret_key}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "success":
        logger.error("Flutterwave initialize failed: %s", data.get("message"))
        raise ValueError(data.get("message", "Flutterwave initialization failed"))

    return data["data"]


def verify_transaction(db: Session, tx_ref: str) -> dict[str, Any]:
    """Verify a Flutterwave transaction by reference.

    Args:
        db: Database session for settings resolution.
        tx_ref: The transaction reference (tx_ref) to verify.

    Returns:
        Dict with transaction status and data from Flutterwave.

    Raises:
        httpx.HTTPStatusError: On non-2xx response.
        ValueError: If secret key is not configured.
    """
    secret_key = _get_secret_key(db)
    if not secret_key:
        raise ValueError("Flutterwave secret key is not configured")

    resp = httpx.get(
        f"{FLUTTERWAVE_API_BASE}/transactions/verify_by_reference",
        params={"tx_ref": tx_ref},
        headers={"Authorization": f"Bearer {secret_key}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "success":
        logger.error("Flutterwave verify failed: %s", data.get("message"))
        raise ValueError(data.get("message", "Flutterwave verification failed"))

    return data["data"]


def verify_webhook_signature(
    body: bytes, signature: str, db: Session | None = None
) -> bool:
    """Verify Flutterwave webhook by comparing the verif-hash header.

    Flutterwave sends a ``verif-hash`` header containing the secret hash
    configured in the dashboard.  Verification is a constant-time string
    comparison against our stored secret hash.

    Args:
        body: Raw request body bytes (unused, kept for API parity).
        signature: Value of the ``verif-hash`` header.
        db: Optional database session for settings resolution.

    Returns:
        True if the signature matches our secret hash.
    """
    secret_hash = _get_secret_hash(db)
    if not secret_hash:
        return False

    return hmac.compare_digest(secret_hash, signature)


def get_public_key(db: Session | None = None) -> str:
    """Return the Flutterwave public key for frontend use."""
    return _get_public_key(db)
