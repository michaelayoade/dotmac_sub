"""Service helpers for admin system API key form/list mutations."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.auth import ApiKey
from app.models.domain_settings import SettingDomain
from app.services import settings_spec
from app.services.auth import hash_api_key
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


class ApiKeyLimitError(ValueError):
    """Raised when an org policy cap (max-TTL or max-per-owner) blocks a create."""


def get_api_key_new_form_context() -> dict:
    """Return default context fragment for API key creation form."""
    return {"error": None}


def parse_scopes(raw: str | None) -> list[str]:
    """Parse the comma/space/newline-separated scopes field into a clean list."""
    if not raw:
        return []
    tokens = raw.replace(",", " ").split()
    seen: list[str] = []
    for token in tokens:
        token = token.strip()
        if token and token not in seen:
            seen.append(token)
    return seen


def _cap_setting(db: Session, key: str) -> int:
    """Read an integer policy cap (0 = disabled). Never raises on bad data."""
    value = settings_spec.resolve_value(db, SettingDomain.auth, key)
    try:
        return max(int(value), 0) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _count_active_keys(db: Session, subscriber_id: str | None) -> int:
    """Active (non-revoked, is_active) key count for an owner.

    ``subscriber_id`` None counts system-owned keys (no subscriber owner).
    """
    query = (
        select(func.count())
        .select_from(ApiKey)
        .where(ApiKey.is_active.is_(True), ApiKey.revoked_at.is_(None))
    )
    if subscriber_id is None:
        query = query.where(ApiKey.subscriber_id.is_(None))
    else:
        query = query.where(ApiKey.subscriber_id == coerce_uuid(subscriber_id))
    return int(db.execute(query).scalar_one())


def create_api_key(
    db: Session,
    *,
    subscriber_id: str | None,
    label: str,
    expires_in: str | None,
    scopes: list[str] | None = None,
) -> str:
    """Create API key and return raw secret once for display.

    ``subscriber_id`` may be None to mint a system/service-owned key with no
    subscriber owner. Enforces two org policy caps (both read here, which also
    keeps them out of the dead-setting lint):

    * ``api_key_max_per_owner`` (>0) — reject once the owner already holds that
      many active keys.
    * ``api_key_max_ttl_days`` (>0) — reject a requested lifetime longer than
      the cap; when no expiry is requested, default it to the cap.
    """
    max_per_owner = _cap_setting(db, "api_key_max_per_owner")
    if max_per_owner > 0:
        active = _count_active_keys(db, subscriber_id)
        if active >= max_per_owner:
            raise ApiKeyLimitError(
                f"Key limit reached: this owner already has {active} active "
                f"key(s) (max {max_per_owner}). Revoke one before creating "
                "another."
            )

    max_ttl_days = _cap_setting(db, "api_key_max_ttl_days")
    requested_days = int(expires_in) if expires_in else None
    if max_ttl_days > 0:
        if requested_days is not None and requested_days > max_ttl_days:
            raise ApiKeyLimitError(
                f"Expiration exceeds the {max_ttl_days}-day maximum allowed by "
                "policy. Choose a shorter lifetime."
            )
        if requested_days is None:
            # "Never" is not allowed once a cap is set — default to the cap.
            requested_days = max_ttl_days

    expires_at = None
    if requested_days is not None:
        expires_at = datetime.now(UTC) + timedelta(days=requested_days)

    raw_key = secrets.token_urlsafe(32)
    # Must match the verification path (auth_dependencies / ApiKeys.generate),
    # which looks keys up via hash_api_key (HMAC-SHA256, legacy-sha256 fallback).
    key_hash = hash_api_key(raw_key)

    api_key = ApiKey(
        subscriber_id=coerce_uuid(subscriber_id) if subscriber_id else None,
        label=label,
        key_hash=key_hash,
        scopes=scopes or [],
        is_active=True,
        expires_at=expires_at,
    )
    db.add(api_key)
    db.commit()
    return raw_key
