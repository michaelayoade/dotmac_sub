import hashlib
import os
import secrets
import time
from datetime import datetime, timezone
from typing import cast

from fastapi import HTTPException, Request
import redis
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models.auth import (
    ApiKey,
    AuthProvider,
    MFAMethod,
    MFAMethodType,
    Session as AuthSession,
    SessionStatus,
    UserCredential,
)
from app.services.common import validate_enum, apply_pagination, apply_ordering, coerce_uuid
from app.services.response import ListResponseMixin
from app.models.radius import RadiusServer
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscriber import Subscriber
from app.services import settings_spec
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyGenerateRequest,
    ApiKeyUpdate,
    MFAMethodCreate,
    MFAMethodUpdate,
    SessionCreate,
    SessionUpdate,
    UserCredentialCreate,
    UserCredentialUpdate,
)


def hash_api_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_API_KEY_WINDOW_SECONDS = 60
_API_KEY_MAX_PER_WINDOW = 5
_REDIS_CLIENT: redis.Redis | None = None


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
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    try:
        client = redis.Redis.from_url(url, decode_responses=True)
        client.ping()
        _REDIS_CLIENT = client
        return client
    except redis.RedisError:
        return None



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
    def create(db: Session, payload: UserCredentialCreate):
        _ensure_subscriber(db, str(payload.subscriber_id))
        if payload.radius_server_id:
            _ensure_radius_server(db, str(payload.radius_server_id))
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "provider" not in fields_set:
            default_provider = settings_spec.resolve_value(
                db, SettingDomain.auth, "default_auth_provider"
            )
            if default_provider:
                data["provider"] = validate_enum(
                    default_provider, AuthProvider, "provider"
                )
        credential = UserCredential(**data)
        db.add(credential)
        db.commit()
        db.refresh(credential)
        return credential

    @staticmethod
    def get(db: Session, credential_id: str):
        credential = db.get(UserCredential, coerce_uuid(credential_id))
        if not credential:
            raise HTTPException(status_code=404, detail="User credential not found")
        return credential

    @staticmethod
    def list(
        db: Session,
        *,
        subscriber_id: str | None = None,
        person_id: str | None = None,
        provider: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
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

    @staticmethod
    def update(db: Session, credential_id: str, payload: UserCredentialUpdate):
        credential = db.get(UserCredential, coerce_uuid(credential_id))
        if not credential:
            raise HTTPException(status_code=404, detail="User credential not found")
        data = payload.model_dump(exclude_unset=True)
        if "subscriber_id" in data:
            _ensure_subscriber(db, str(data["subscriber_id"]))
        if "radius_server_id" in data and data["radius_server_id"]:
            _ensure_radius_server(db, str(data["radius_server_id"]))
        for key, value in data.items():
            setattr(credential, key, value)
        db.commit()
        db.refresh(credential)
        return credential

    @staticmethod
    def delete(db: Session, credential_id: str):
        credential = db.get(UserCredential, coerce_uuid(credential_id))
        if not credential:
            raise HTTPException(status_code=404, detail="User credential not found")
        credential.is_active = False
        db.commit()


class MFAMethods(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: MFAMethodCreate):
        _ensure_subscriber(db, str(payload.subscriber_id))
        if payload.is_primary:
            db.query(MFAMethod).filter(
                MFAMethod.subscriber_id == payload.subscriber_id,
                MFAMethod.is_primary.is_(True),
            ).update({"is_primary": False})
        method = MFAMethod(**payload.model_dump())
        db.add(method)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Primary MFA method already exists for this user",
            ) from exc
        db.refresh(method)
        return method

    @staticmethod
    def get(db: Session, method_id: str):
        method = db.get(MFAMethod, coerce_uuid(method_id))
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")
        return method

    @staticmethod
    def list(
        db: Session,
        *,
        subscriber_id: str | None = None,
        person_id: str | None = None,
        method_type: str | None,
        is_primary: bool | None,
        enabled: bool | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
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
    def update(db: Session, method_id: str, payload: MFAMethodUpdate):
        method = db.get(MFAMethod, coerce_uuid(method_id))
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")
        data = payload.model_dump(exclude_unset=True)
        if "subscriber_id" in data:
            _ensure_subscriber(db, str(data["subscriber_id"]))
        if data.get("is_primary"):
            subscriber_id = data.get("subscriber_id", method.subscriber_id)
            db.query(MFAMethod).filter(
                MFAMethod.subscriber_id == subscriber_id,
                MFAMethod.id != method.id,
                MFAMethod.is_primary.is_(True),
            ).update({"is_primary": False})
        for key, value in data.items():
            setattr(method, key, value)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Primary MFA method already exists for this user",
            ) from exc
        db.refresh(method)
        return method

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
    def create(db: Session, payload: SessionCreate):
        _ensure_subscriber(db, str(payload.subscriber_id))
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.auth, "default_session_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, SessionStatus, "status"
                )
        session = AuthSession(**data)
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def get(db: Session, session_id: str):
        session = db.get(AuthSession, coerce_uuid(session_id))
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    @staticmethod
    def list(
        db: Session,
        *,
        subscriber_id: str | None = None,
        person_id: str | None = None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(AuthSession)
        resolved_subscriber_id = subscriber_id or person_id
        if resolved_subscriber_id:
            query = query.filter(
                AuthSession.subscriber_id == coerce_uuid(resolved_subscriber_id)
            )
        if status:
            query = query.filter(
                AuthSession.status
                == validate_enum(status, SessionStatus, "status")
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
    def update(db: Session, session_id: str, payload: SessionUpdate):
        session = db.get(AuthSession, coerce_uuid(session_id))
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        data = payload.model_dump(exclude_unset=True)
        if "subscriber_id" in data:
            _ensure_subscriber(db, str(data["subscriber_id"]))
        for key, value in data.items():
            setattr(session, key, value)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def delete(db: Session, session_id: str):
        session = db.get(AuthSession, coerce_uuid(session_id))
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        session.status = SessionStatus.revoked
        session.revoked_at = datetime.now(timezone.utc)
        db.commit()


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
        max_per_window = _auth_int_setting(db, "api_key_rate_max", _API_KEY_MAX_PER_WINDOW)
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
        *,
        subscriber_id: str | None = None,
        person_id: str | None = None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
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
        api_key.revoked_at = datetime.now(timezone.utc)
        db.commit()

    @staticmethod
    def revoke(db: Session, key_id: str):
        ApiKeys.delete(db, key_id)


user_credentials = UserCredentials()
mfa_methods = MFAMethods()
sessions = Sessions()
api_keys = ApiKeys()
