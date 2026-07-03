"""Service helpers for admin system API key pages."""

from __future__ import annotations

import logging

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.auth import ApiKey
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


def list_api_keys_for_subscriber(
    db: Session, subscriber_id: str | None
) -> list[ApiKey]:
    """Return manageable API keys sorted by most recent.

    Always includes system/service-owned keys (no subscriber owner) so they are
    visible and manageable from the permissioned admin page; when a
    ``subscriber_id`` is supplied its own keys are included too.
    """
    condition: ColumnElement[bool] = ApiKey.subscriber_id.is_(None)
    if subscriber_id:
        condition = or_(condition, ApiKey.subscriber_id == coerce_uuid(subscriber_id))
    keys = (
        db.execute(select(ApiKey).where(condition).order_by(ApiKey.created_at.desc()))
        .scalars()
        .all()
    )
    return list(keys)
