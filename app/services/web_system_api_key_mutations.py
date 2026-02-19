"""Mutation helpers for admin system API keys."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.auth import ApiKey
from app.services.common import coerce_uuid


def revoke_api_key(db: Session, *, key_id: str) -> bool:
    """Revoke API key by id. Returns True when key exists."""
    api_key = db.get(ApiKey, coerce_uuid(key_id))
    if not api_key:
        return False
    api_key.revoked_at = datetime.now(timezone.utc)
    api_key.is_active = False
    db.commit()
    return True
