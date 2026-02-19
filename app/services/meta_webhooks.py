"""Meta webhook processing service - CRM functionality removed.

This module previously handled Facebook/Instagram webhooks for CRM.
Now stubbed as CRM functionality has been removed.
"""

import hashlib
import hmac
from datetime import datetime
from typing import cast

from sqlalchemy.orm import Session

from app.logging import get_logger
from app.config import settings

logger = get_logger(__name__)


def verify_webhook_signature(
    payload: bytes,
    signature_header: str | None,
    secret: str | None = None,
) -> bool:
    """Verify webhook signature from Meta platform.

    This verification still works for any remaining Meta webhook endpoints.
    """
    if not signature_header:
        return False

    if not secret:
        secret = cast(str | None, getattr(settings, "meta_app_secret", None))
    if not secret:
        logger.warning("meta_webhook_signature_no_secret")
        return False

    expected_signature = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    actual_signature = signature_header
    if actual_signature.startswith("sha256="):
        actual_signature = actual_signature[7:]

    return hmac.compare_digest(expected_signature, actual_signature)


def process_messenger_webhook(db: Session, payload) -> list:
    """Process Facebook Messenger webhook - CRM removed, no-op."""
    logger.info("messenger_webhook_received_but_crm_removed")
    return []


def process_instagram_webhook(db: Session, payload) -> list:
    """Process Instagram webhook - CRM removed, no-op."""
    logger.info("instagram_webhook_received_but_crm_removed")
    return []


def receive_facebook_message(db: Session, **kwargs) -> None:
    """Receive Facebook message - CRM removed, no-op."""
    pass


def receive_instagram_message(db: Session, **kwargs) -> None:
    """Receive Instagram message - CRM removed, no-op."""
    pass
