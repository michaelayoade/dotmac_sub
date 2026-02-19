"""Service helpers for admin system API key form/list mutations."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.auth import ApiKey
from app.services.auth_flow import hash_password
from app.services.common import coerce_uuid


def get_api_key_new_form_context() -> dict:
    """Return default context fragment for API key creation form."""
    return {"error": None}


def create_api_key(
    db: Session,
    *,
    subscriber_id: str,
    label: str,
    expires_in: str | None,
) -> str:
    """Create API key and return raw secret once for display."""
    raw_key = secrets.token_urlsafe(32)
    key_hash = hash_password(raw_key)

    expires_at = None
    if expires_in:
        days = int(expires_in)
        expires_at = datetime.now(timezone.utc) + timedelta(days=days)

    api_key = ApiKey(
        subscriber_id=coerce_uuid(subscriber_id),
        label=label,
        key_hash=key_hash,
        is_active=True,
        expires_at=expires_at,
    )
    db.add(api_key)
    db.commit()
    return raw_key
