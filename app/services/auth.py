import hashlib
import hmac
import logging
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import cast

import redis
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.auth import (
    ApiKey,
    AuthProvider,
    MFAMethod,
    MFAMethodType,
    MFARecoveryCode,
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
from app.services import auth_cache
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    validate_enum,
)
from app.services.redis_client import get_redis
from app.services.response import ListResponseMixin

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
    def delete(db: Session, method_id: str, *, actor_id: str, reason: str):
        """Disable one MFA method, its recovery codes and the audit atomically.

        Recovery codes are keyed to a method (``mfa_method_id``), so disabling a
        method also deactivates that method's unused codes: they cannot be
        redeemed while the method is off, and must not revive if an enrollment
        later re-enables the same row. All state changes and the audit event
        are staged and committed in one transaction; any failure rolls back.
        """

        if not actor_id or not actor_id.strip():
            raise HTTPException(status_code=403, detail="Administrator is required")
        from app.services import mfa_removal

        cleaned_reason = mfa_removal.normalize_reason(reason)
        if cleaned_reason is None:
            raise HTTPException(
                status_code=422, detail=mfa_removal.REASON_REQUIRED_MESSAGE
            )
        from app.models.auth import Session as _AuthSession
        from app.services.credential_administration import (
            PrincipalType,
            _lock_principal,
        )
        from app.services.events import emit_event
        from app.services.events.types import EventType

        def principal_of(row: MFAMethod) -> tuple[PrincipalType, uuid.UUID]:
            for kind, value in (
                (PrincipalType.subscriber, row.subscriber_id),
                (PrincipalType.system_user, row.system_user_id),
                (PrincipalType.reseller_user, row.reseller_user_id),
            ):
                if value is not None:
                    return kind, value
            raise HTTPException(status_code=404, detail="MFA method not found")

        try:
            probe = db.get(MFAMethod, coerce_uuid(method_id))
            if not probe:
                raise HTTPException(status_code=404, detail="MFA method not found")
            kind, principal_id = principal_of(probe)
            if kind is not PrincipalType.subscriber:
                # Staff and reseller factors are removed by their owners
                # (staff provisioning / reseller management), which hold the
                # matching permission model. Refuse before any lock or write.
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Staff and reseller MFA factors are managed by their "
                        "own provisioning flows (staff provisioning, reseller "
                        "management)."
                    ),
                )
            # Lock order matches login/complete_mfa: principal, then method,
            # then recovery codes. State is re-read after the locks.
            _lock_principal(db, kind, principal_id)
            method = db.scalar(
                select(MFAMethod)
                .where(MFAMethod.id == probe.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if not method or principal_of(method) != (kind, principal_id):
                raise HTTPException(status_code=404, detail="MFA method not found")
            if method.is_active or method.enabled or method.is_primary:
                codes_deactivated = (
                    db.query(MFARecoveryCode)
                    .filter(
                        MFARecoveryCode.mfa_method_id == method.id,
                        MFARecoveryCode.is_active.is_(True),
                    )
                    .update({"is_active": False}, synchronize_session=False)
                )
                method.is_active = False
                method.enabled = False
                method.is_primary = False
                # Drop the seed so a holder of the old secret cannot re-confirm.
                method.secret = None
                sessions_revoked = (
                    db.query(_AuthSession)
                    .filter(
                        getattr(_AuthSession, f"{kind.value}_id") == principal_id,
                        _AuthSession.status == SessionStatus.active,
                    )
                    .update(
                        {
                            "status": SessionStatus.revoked,
                            "revoked_at": datetime.now(UTC),
                        },
                        synchronize_session=False,
                    )
                )
                db.flush()
                evidence: dict[str, object] = {
                    "principal_type": kind.value,
                    "principal_id": str(principal_id),
                    "method_type": method.method_type.value,
                    "recovery_codes_deactivated": int(codes_deactivated),
                    "sessions_revoked": int(sessions_revoked),
                    "removal_reason": cleaned_reason,
                }
                stage_audit_event(
                    db,
                    action="auth.mfa_method.disabled",
                    entity_type="mfa_method",
                    entity_id=str(method.id),
                    actor=AuditActor.user(actor_id.strip()),
                    metadata=evidence,
                )
                emit_event(
                    db,
                    EventType.credential_administration_completed,
                    {
                        **{k: v for k, v in evidence.items() if k != "removal_reason"},
                        "schema_version": 1,
                        "aggregate_type": "mfa_method",
                        "aggregate_id": str(method.id),
                        "transition": "mfa_method_disabled",
                        "invalidate_sessions": True,
                        "removed_at": datetime.now(UTC).isoformat(),
                        "notify_mfa_removal": True,
                    },
                    actor=actor_id.strip(),
                    subscriber_id=method.subscriber_id,
                )
            db.commit()
        except OperationalError as exc:
            db.rollback()
            if mfa_removal.is_lock_timeout(exc):
                raise HTTPException(
                    status_code=503, detail=mfa_removal.LOCK_TIMEOUT_MESSAGE
                ) from exc
            raise
        except BaseException:
            db.rollback()
            raise


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
    def delete(db: Session, session_id: str):
        session = db.get(AuthSession, coerce_uuid(session_id))
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        principal_type = "system_user" if session.system_user_id else "subscriber"
        principal_id = str(session.system_user_id or session.subscriber_id)
        session.status = SessionStatus.revoked
        session.revoked_at = datetime.now(UTC)
        db.commit()
        auth_cache.invalidate_session_context(
            str(session.id),
            principal_type=principal_type,
            principal_id=principal_id,
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
