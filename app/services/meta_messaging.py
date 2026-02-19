"""Meta messaging service - CRM functionality removed.

This module previously handled Facebook/Instagram messaging for CRM.
Now stubbed as CRM functionality has been removed.
"""

from sqlalchemy.orm import Session

from app.logging import get_logger

logger = get_logger(__name__)


def send_facebook_message(db: Session, **kwargs) -> dict:
    """Send a message via Facebook Messenger - CRM removed, no-op."""
    logger.warning("send_facebook_message_called_but_crm_removed")
    return {}


def send_instagram_message(db: Session, **kwargs) -> dict:
    """Send a message via Instagram DM - CRM removed, no-op."""
    logger.warning("send_instagram_message_called_but_crm_removed")
    return {}


def send_facebook_message_sync(db: Session, **kwargs) -> dict:
    """Sync wrapper for send_facebook_message - CRM removed, no-op."""
    return send_facebook_message(db, **kwargs)


def send_instagram_message_sync(db: Session, **kwargs) -> dict:
    """Sync wrapper for send_instagram_message - CRM removed, no-op."""
    return send_instagram_message(db, **kwargs)
