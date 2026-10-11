import hashlib
import hmac
import logging
import secrets
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import redis
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.models.auth import (
    ApiKey,
    AuthProvider,
    MFAMethod,
    MFAMethodType,
    SessionStatus,
    UserCredential,
)
from app.models.auth import (
    Session as AuthSession,
)
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.radius import RadiusServer
from app.models.subscriber import Subscriber
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyGenerateRequest,
    ApiKeyUpdate,
)
from app.services.audit_adapter import AuditActor
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    validate_enum,
)
from app.services.owner_commands import CommandContext
from app.services.redis_client import get_redis
from app.services.response import ListResponseMixin

if TYPE_CHECKING:
    from app.services.session_manager import SessionRevocationOutcome

logger = logging.getLogger(__name__)


# API-key hashing: HMAC-SHA256 keyed with a server secret (defense-in-depth
# over the historical unsalted sha256 — an unsalted digest is offline-guessable
# / rainbow-able if the DB leaks; an HMAC is not without the key). New hashes
# carry an ``hmac256:`` prefix so the verify path can tell schemes apart and
# transparently upgrade legacy rows on next use (no key rotation needed).
_HMAC_SCHEME_PREFIX = "hmac256:"
_HMAC_DERIVATION_INFO = b"api-key-hash-v1"


def _api_key_hmac_secret() -> bytes | None:
    """Derive a dedicated HMAC subkey from the credential-encryption key.

    Reuses the always-present Fernet key (connector encryption already depends
    on it) but derives a *separate* subkey rather than using it directly, so
    the two purposes stay cryptographically independent. Returns None only when
    no encryption key is configured (dev/test) — then we fall back to legacy
    sha256 so nothing breaks.
    """
    from app.services.credential_crypto import get_encryption_key

    key = get_encryption_key()
    if not key:
        return None
    key_bytes = key if isinstance(key, bytes) else str(key).encode("utf-8")
    return hmac.new(key_bytes, _HMAC_DERIVATION_INFO, hashlib.sha256).digest()


def _legacy_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_api_key(value: str) -> str:
    """Hash a raw API key for storage/lookup.

    HMAC-SHA256 (prefixed) when a server secret is available; legacy unsalted
    sha256 only when it is not (keeps dev/test working). Prod always has the
    key, so prod always stores the HMAC form.
    """
    secret = _api_key_hmac_secret()
    if secret is None:
        return _legacy_sha256(value)
    digest = hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{_HMAC_SCHEME_PREFIX}{digest}"


def hash_api_key_candidates(value: str) -> list[str]:
    """Every stored-hash form a raw key may currently match: the preferred
    (HMAC) hash first, plus the legacy sha256 so pre-migration rows still
    authenticate until they are rehashed-on-use."""
    candidates = [hash_api_key(value)]
    legacy = _legacy_sha256(value)
    if legacy not in candidates:
        candidates.append(legacy)
    return candidates


def is_legacy_api_key_hash(stored_hash: str | None) -> bool:
    """True when a stored hash is the pre-HMAC unsalted-sha256 form."""
    if not stored_hash:
        return False
    return not stored_hash.startswith(_HMAC_SCHEME_PREFIX)


_API_KEY_WINDOW_SECONDS = 60
_API_KEY_MAX_PER_WINDOW = 5


def _auth_setting(db: Session, key: str) -> str | None:
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.auth)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        return None
    if setting.value_text is not None:
        return setting.value_text
    if setting.value_json is not None:
        return str(setting.value_json)
    return None


def _auth_int_setting(db: Session, key: str, default: int) -> int:
    value = _auth_setting(db, key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_redis_client() -> redis.Redis | None:
    """Get Redis client for auth rate limiting.

    Uses the centralized Redis client with circuit breaker protection.
    """
    return get_redis()


def _ensure_subscriber(db: Session, subscriber_id: str):
    subscriber = db.get(Subscriber, coerce_uuid(subscriber_id))
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber not found")


def _ensure_person(db: Session, person_id: str):
    """Backwards-compatible helper: people are subscribers in this codebase."""
    _ensure_subscriber(db, person_id)


def _ensure_radius_server(db: Session, server_id: str):
    server = db.get(RadiusServer, coerce_uuid(server_id))
    if not server:
        raise HTTPException(status_code=404, detail="Radius server not found")


class UserCredentials(ListResponseMixin):
    @staticmethod
    def get(db: Session, credential_id: str):
        credential = db.get(UserCredential, coerce_uuid(credential_id))
        if not credential:
            raise HTTPException(status_code=404, detail="User credential not found")
        return credential

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None = None,
        person_id: str | None = None,
        provider: str | None = None,
        is_active: bool | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ):
        query = db.query(UserCredential)
        resolved_subscriber_id = subscriber_id or person_id
        if resolved_subscriber_id:
            query = query.filter(
                UserCredential.subscriber_id == coerce_uuid(resolved_subscriber_id)
            )
        if provider:
            query = query.filter(
                UserCredential.provider
                == validate_enum(provider, AuthProvider, "provider")
            )
        if is_active is None:
            query = query.filter(UserCredential.is_active.is_(True))
        else:
            query = query.filter(UserCredential.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": UserCredential.created_at,
                "username": UserCredential.username,
                "last_login_at": UserCredential.last_login_at,
            },
        )
        return apply_pagination(query, limit, offset).all()


class MFAMethods(ListResponseMixin):
    @staticmethod
    def get(db: Session, method_id: str):
        method = db.get(MFAMethod, coerce_uuid(method_id))
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")
        return method

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None = None,
        person_id: str | None = None,
        method_type: str | None = None,
        is_primary: bool | None = None,
        enabled: bool | None = None,
        is_active: bool | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ):
        query = db.query(MFAMethod)
        resolved_subscriber_id = subscriber_id or person_id
        if resolved_subscriber_id:
            query = query.filter(
                MFAMethod.subscriber_id == coerce_uuid(resolved_subscriber_id)
            )
        if method_type:
            query = query.filter(
                MFAMethod.method_type
                == validate_enum(method_type, MFAMethodType, "method_type")
            )
        if is_primary is not None:
            query = query.filter(MFAMethod.is_primary == is_primary)
        if enabled is not None:
            query = query.filter(MFAMethod.enabled == enabled)
        if is_active is None:
            query = query.filter(MFAMethod.is_active.is_(True))
        else:
            query = query.filter(MFAMethod.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": MFAMethod.created_at,
                "method_type": MFAMethod.method_type,
                "is_primary": MFAMethod.is_primary,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def delete(db: Session, method_id: str):
        method = db.get(MFAMethod, coerce_uuid(method_id))
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")
        method.is_active = False
        method.enabled = False
        method.is_primary = False
        db.commit()


class Sessions(ListResponseMixin):
    @staticmethod
    def get(db: Session, session_id: str):
        session = db.get(AuthSession, coerce_uuid(session_id))
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None = None,
        status: str | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
        person_id: str | None = None,
    ):
        query = db.query(AuthSession)
        resolved_subscriber_id = subscriber_id or person_id
        if resolved_subscriber_id:
            query = query.filter(
                AuthSession.subscriber_id == coerce_uuid(resolved_subscriber_id)
            )
        if status:
            query = query.filter(
                AuthSession.status == validate_enum(status, SessionStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": AuthSession.created_at,
                "last_seen_at": AuthSession.last_seen_at,
                "status": AuthSession.status,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def delete(
        db: Session, session_id: str, *, actor: AuditActor
    ) -> "SessionRevocationOutcome":
        from app.services import session_manager

        return session_manager.revoke_authentication_sessions(
            db,
            session_manager.RevokeSessionsCommand(
                context=CommandContext.system(
                    actor=actor.actor_id or "system",
                    scope="administrative_sessions",
                    reason="administrative_session_revocation",
                ),
                intent=session_manager.RevocationIntent.ADMIN,
                actor=actor,
                session_id=session_manager.target_session_uuid(session_id),
            ),
        )


class ApiKeys(ListResponseMixin):
    @staticmethod
    def generate_with_rate_limit(
        db: Session, payload: ApiKeyGenerateRequest, request: Request | None
    ):
        client_ip = "unknown"
        if request is not None and request.client:
            client_ip = request.client.host
        window_seconds = _auth_int_setting(
            db, "api_key_rate_window_seconds", _API_KEY_WINDOW_SECONDS
        )
        max_per_window = _auth_int_setting(
            db, "api_key_rate_max", _API_KEY_MAX_PER_WINDOW
        )
        redis_client = _get_redis_client()
        if not redis_client:
            raise HTTPException(
                status_code=503,
                detail="Rate limiting unavailable (Redis required)",
            )
        window = max(window_seconds, 1)
        key = f"api_key_rl:{client_ip}:{int(time.time() // window)}"
        try:
            # redis-py stubs currently type incr() as Any/Awaitable[Any] in some cases.
            count = cast(int, redis_client.incr(key))
            if count == 1:
                redis_client.expire(key, window)
            if count > max(max_per_window, 1):
                raise HTTPException(status_code=429, detail="Rate limit exceeded")
        except redis.RedisError as exc:
            raise HTTPException(
                status_code=503,
                detail="Rate limiting unavailable (Redis error)",
            ) from exc
        api_key, raw_key = ApiKeys.generate(db, payload)
        return {"key": raw_key, "api_key": api_key}

    @staticmethod
    def generate(db: Session, payload: ApiKeyGenerateRequest):
        raw_key = secrets.token_urlsafe(32)
        data = payload.model_dump()
        data["key_hash"] = hash_api_key(raw_key)
        data.setdefault("is_active", True)
        if data.get("subscriber_id"):
            _ensure_subscriber(db, str(data["subscriber_id"]))
        api_key = ApiKey(**data)
        db.add(api_key)
        db.commit()
        db.refresh(api_key)
        return api_key, raw_key

    @staticmethod
    def create(db: Session, payload: ApiKeyCreate):
        if payload.subscriber_id:
            _ensure_subscriber(db, str(payload.subscriber_id))
        data = payload.model_dump()
        data["key_hash"] = hash_api_key(data["key_hash"])
        api_key = ApiKey(**data)
        db.add(api_key)
        db.commit()
        db.refresh(api_key)
        return api_key

    @staticmethod
    def get(db: Session, key_id: str):
        api_key = db.get(ApiKey, coerce_uuid(key_id))
        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")
        return api_key

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None = None,
        person_id: str | None = None,
        is_active: bool | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ):
        query = db.query(ApiKey)
        resolved_subscriber_id = subscriber_id or person_id
        if resolved_subscriber_id:
            query = query.filter(
                ApiKey.subscriber_id == coerce_uuid(resolved_subscriber_id)
            )
        if is_active is None:
            query = query.filter(ApiKey.is_active.is_(True))
        else:
            query = query.filter(ApiKey.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ApiKey.created_at, "label": ApiKey.label},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, key_id: str, payload: ApiKeyUpdate):
        api_key = db.get(ApiKey, coerce_uuid(key_id))
        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")
        data = payload.model_dump(exclude_unset=True)
        if "subscriber_id" in data and data["subscriber_id"] is not None:
            _ensure_subscriber(db, str(data["subscriber_id"]))
        if "key_hash" in data and data["key_hash"]:
            data["key_hash"] = hash_api_key(data["key_hash"])
        for key, value in data.items():
            setattr(api_key, key, value)
        db.commit()
        db.refresh(api_key)
        return api_key

    @staticmethod
    def delete(db: Session, key_id: str):
        api_key = db.get(ApiKey, coerce_uuid(key_id))
        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")
        api_key.is_active = False
        api_key.revoked_at = datetime.now(UTC)
        db.commit()

    @staticmethod
    def revoke(db: Session, key_id: str):
        ApiKeys.delete(db, key_id)


user_credentials = UserCredentials()
mfa_methods = MFAMethods()
sessions = Sessions()
api_keys = ApiKeys()
