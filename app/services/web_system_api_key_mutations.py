"""Mutation helpers for admin system API keys."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.auth import ApiKey
from app.services.auth import hash_api_key
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


def _in_scope(api_key: ApiKey, subscriber_id: str | None) -> bool:
    """Whether a caller scoped to ``subscriber_id`` may act on ``api_key``.

    System-owned keys (no subscriber owner) are managed by any permissioned
    admin — the mutation routes are already gated behind
    ``system:settings:write``. Subscriber-owned keys stay scoped to their owner
    so a staffer can't revoke/rotate another principal's key by guessing ids.
    """
    if api_key.subscriber_id is None:
        return True
    if subscriber_id is None:
        return True
    return str(api_key.subscriber_id) == str(coerce_uuid(subscriber_id))


def revoke_api_key(
    db: Session, *, key_id: str, subscriber_id: str | None = None
) -> bool:
    """Revoke API key by id. Returns True when the key exists AND is in scope
    for the caller (own subscriber-owned key, or any system-owned key)."""
    api_key = db.get(ApiKey, coerce_uuid(key_id))
    if not api_key:
        return False
    if not _in_scope(api_key, subscriber_id):
        return False
    api_key.revoked_at = datetime.now(UTC)
    api_key.is_active = False
    db.commit()
    return True


def rotate_api_key(
    db: Session, *, key_id: str, subscriber_id: str | None = None
) -> str | None:
    """Rotate an active key's secret in place, returning the new raw key once.

    Generates a fresh ``secrets.token_urlsafe`` secret, re-hashes via
    ``hash_api_key`` and overwrites ``key_hash`` — the old secret stops working
    immediately. Label/scopes/owner/expiry are preserved. Returns None when the
    key is missing, out of scope, or already revoked/inactive.
    """
    api_key = db.get(ApiKey, coerce_uuid(key_id))
    if not api_key:
        return None
    if not _in_scope(api_key, subscriber_id):
        return None
    if api_key.revoked_at is not None or not api_key.is_active:
        return None
    raw_key = secrets.token_urlsafe(32)
    api_key.key_hash = hash_api_key(raw_key)
    db.commit()
    return raw_key
