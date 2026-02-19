"""Service helpers for admin system webhook pages."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.webhook import WebhookDelivery, WebhookDeliveryStatus, WebhookEndpoint


def get_webhooks_list_data(db: Session) -> dict[str, object]:
    """Return webhook endpoints and 24h delivery statistics."""
    endpoints = db.execute(
        select(WebhookEndpoint).order_by(WebhookEndpoint.created_at.desc())
    ).scalars().all()

    active_count = (
        db.scalar(
            select(func.count())
            .select_from(WebhookEndpoint)
            .where(WebhookEndpoint.is_active.is_(True))
        )
        or 0
    )

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    delivery_count_24h = (
        db.scalar(
            select(func.count())
            .select_from(WebhookDelivery)
            .where(WebhookDelivery.created_at >= cutoff)
        )
        or 0
    )
    failed_count_24h = (
        db.scalar(
            select(func.count())
            .select_from(WebhookDelivery)
            .where(WebhookDelivery.created_at >= cutoff)
            .where(WebhookDelivery.status == WebhookDeliveryStatus.failed)
        )
        or 0
    )

    return {
        "endpoints": endpoints,
        "active_count": active_count,
        "delivery_count_24h": delivery_count_24h,
        "failed_count_24h": failed_count_24h,
    }
