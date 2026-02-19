"""Service helpers for admin system API key pages."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import ApiKey
from app.services.common import coerce_uuid


def list_api_keys_for_subscriber(db: Session, subscriber_id: str | None) -> list[ApiKey]:
    """Return API keys for a subscriber sorted by most recent."""
    if not subscriber_id:
        return []
    keys = db.execute(
        select(ApiKey)
        .where(ApiKey.subscriber_id == coerce_uuid(subscriber_id))
        .order_by(ApiKey.created_at.desc())
    ).scalars().all()
    return list(keys)
