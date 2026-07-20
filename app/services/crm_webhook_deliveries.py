from __future__ import annotations

import logging
import uuid

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.crm_webhook_delivery import CrmWebhookDelivery

logger = logging.getLogger(__name__)


def claim_delivery(
    db: Session,
    delivery_id: uuid.UUID,
    event_type: str,
    *,
    event_id: str | None = None,
) -> bool:
    """Record a CRM webhook delivery; return False when already claimed.

    The dedup store fails open because a database-side dedup outage must never
    drop a real upstream event.
    """
    try:
        if db.get(CrmWebhookDelivery, delivery_id) is not None:
            return False
        db.add(
            CrmWebhookDelivery(
                delivery_id=delivery_id,
                event_id=event_id,
                event_type=event_type or "unknown",
                status="processed",
            )
        )
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
    except SQLAlchemyError:
        db.rollback()
        logger.exception("crm_webhook_dedup_store_error event=%s", event_type)
        return True
