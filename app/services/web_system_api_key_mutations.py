"""Mutation helpers for admin system API keys."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.auth import ApiKey
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


def revoke_api_key(
    db: Session, *, key_id: str, subscriber_id: str | None = None
) -> bool:
    """Revoke API key by id. Returns True when the key exists AND (when a
    ``subscriber_id`` scope is supplied) it belongs to that subscriber.

    Scoping prevents a staffer from revoking another principal's key by id — the
    admin list/create paths are already scoped to the caller's own keys.
    """
    api_key = db.get(ApiKey, coerce_uuid(key_id))
    if not api_key:
        return False
    if subscriber_id is not None and str(api_key.subscriber_id) != str(
        coerce_uuid(subscriber_id)
    ):
        return False
    api_key.revoked_at = datetime.now(UTC)
    api_key.is_active = False
    db.commit()
    return True
