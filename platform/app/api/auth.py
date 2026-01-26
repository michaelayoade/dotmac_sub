import os
import time

import redis
from app.schemas.common import ListResponse
from app.api.response import list_response

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyGenerateRequest,
    ApiKeyGenerateResponse,
    ApiKeyRead,
    ApiKeyUpdate,
    MFAMethodCreate,
    MFAMethodRead,
    MFAMethodUpdate,
    SessionCreate,
    SessionRead,
    SessionUpdate,
    UserCredentialCreate,
    UserCredentialRead,
    UserCredentialUpdate,
)
from app.services import auth as auth_service
from app.models.domain_settings import DomainSetting, SettingDomain

router = APIRouter()

_API_KEY_WINDOW_SECONDS = 60
_API_KEY_MAX_PER_WINDOW = 5
_API_KEY_RATE_BUCKETS: dict[str, list[float]] = {}
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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post(
    "/user-credentials",
    response_model=UserCredentialRead,
    status_code=status.HTTP_201_CREATED,
    tags=["user-credentials"],
)
def create_user_credential(payload: UserCredentialCreate, db: Session = Depends(get_db)):
    return auth_service.user_credentials.create(db, payload)


@router.get(
    "/user-credentials/{credential_id}",
    response_model=UserCredentialRead,
    tags=["user-credentials"],
)
def get_user_credential(credential_id: str, db: Session = Depends(get_db)):
    return auth_service.user_credentials.get(db, credential_id)


@router.get(
    "/user-credentials",
    response_model=ListResponse[UserCredentialRead],
    tags=["user-credentials"],
)
def list_user_credentials(
    person_id: str | None = None,
    provider: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = auth_service.user_credentials.list(
        db, person_id, provider, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.patch(
    "/user-credentials/{credential_id}",
    response_model=UserCredentialRead,
    tags=["user-credentials"],
)
def update_user_credential(
    credential_id: str, payload: UserCredentialUpdate, db: Session = Depends(get_db)
):
    return auth_service.user_credentials.update(db, credential_id, payload)


@router.delete(
    "/user-credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["user-credentials"],
)
def delete_user_credential(credential_id: str, db: Session = Depends(get_db)):
    auth_service.user_credentials.delete(db, credential_id)


@router.post(
    "/mfa-methods",
    response_model=MFAMethodRead,
    status_code=status.HTTP_201_CREATED,
    tags=["mfa-methods"],
)
def create_mfa_method(payload: MFAMethodCreate, db: Session = Depends(get_db)):
    return auth_service.mfa_methods.create(db, payload)


@router.get(
    "/mfa-methods/{method_id}",
    response_model=MFAMethodRead,
    tags=["mfa-methods"],
)
def get_mfa_method(method_id: str, db: Session = Depends(get_db)):
    return auth_service.mfa_methods.get(db, method_id)


@router.get(
    "/mfa-methods",
    response_model=ListResponse[MFAMethodRead],
    tags=["mfa-methods"],
)
def list_mfa_methods(
    person_id: str | None = None,
    method_type: str | None = None,
    is_primary: bool | None = None,
    enabled: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = auth_service.mfa_methods.list(
        db,
        person_id,
        method_type,
        is_primary,
        enabled,
        order_by,
        order_dir,
        limit,
        offset,
    )
    return list_response(items, limit, offset)


@router.patch(
    "/mfa-methods/{method_id}",
    response_model=MFAMethodRead,
    tags=["mfa-methods"],
)
def update_mfa_method(
    method_id: str, payload: MFAMethodUpdate, db: Session = Depends(get_db)
):
    return auth_service.mfa_methods.update(db, method_id, payload)


@router.delete(
    "/mfa-methods/{method_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["mfa-methods"],
)
def delete_mfa_method(method_id: str, db: Session = Depends(get_db)):
    auth_service.mfa_methods.delete(db, method_id)


@router.post(
    "/sessions",
    response_model=SessionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["sessions"],
)
def create_session(payload: SessionCreate, db: Session = Depends(get_db)):
    return auth_service.sessions.create(db, payload)


@router.get(
    "/sessions/{session_id}",
    response_model=SessionRead,
    tags=["sessions"],
)
def get_session(session_id: str, db: Session = Depends(get_db)):
    return auth_service.sessions.get(db, session_id)


@router.get(
    "/sessions",
    response_model=ListResponse[SessionRead],
    tags=["sessions"],
)
def list_sessions(
    person_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = auth_service.sessions.list(
        db, person_id, status, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.patch(
    "/sessions/{session_id}",
    response_model=SessionRead,
    tags=["sessions"],
)
def update_session(
    session_id: str, payload: SessionUpdate, db: Session = Depends(get_db)
):
    return auth_service.sessions.update(db, session_id, payload)


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["sessions"],
)
def delete_session(session_id: str, db: Session = Depends(get_db)):
    auth_service.sessions.delete(db, session_id)


@router.post(
    "/api-keys",
    response_model=ApiKeyRead,
    status_code=status.HTTP_201_CREATED,
    tags=["api-keys"],
)
def create_api_key(payload: ApiKeyCreate, db: Session = Depends(get_db)):
    return auth_service.api_keys.create(db, payload)


@router.post(
    "/api-keys/generate",
    response_model=ApiKeyGenerateResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["api-keys"],
)
def generate_api_key(
    payload: ApiKeyGenerateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    client_ip = request.client.host if request.client else "unknown"
    window_seconds = _auth_int_setting(
        db, "api_key_rate_window_seconds", _API_KEY_WINDOW_SECONDS
    )
    max_per_window = _auth_int_setting(
        db, "api_key_rate_max", _API_KEY_MAX_PER_WINDOW
    )
    redis_client = _get_redis_client()
    if redis_client:
        window = max(window_seconds, 1)
        key = f"api_key_rl:{client_ip}:{int(time.time() // window)}"
        try:
            count = redis_client.incr(key)
            if count == 1:
                redis_client.expire(key, window)
            if count > max(max_per_window, 1):
                raise HTTPException(status_code=429, detail="Rate limit exceeded")
        except redis.RedisError:
            redis_client = None
    if not redis_client:
        now = time.monotonic()
        window_start = now - max(window_seconds, 1)
        timestamps = [
            t for t in _API_KEY_RATE_BUCKETS.get(client_ip, []) if t >= window_start
        ]
        if len(timestamps) >= max(max_per_window, 1):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        timestamps.append(now)
        _API_KEY_RATE_BUCKETS[client_ip] = timestamps
    api_key, raw_key = auth_service.api_keys.generate(db, payload)
    return {"key": raw_key, "api_key": api_key}


@router.get(
    "/api-keys/{key_id}",
    response_model=ApiKeyRead,
    tags=["api-keys"],
)
def get_api_key(key_id: str, db: Session = Depends(get_db)):
    return auth_service.api_keys.get(db, key_id)


@router.get(
    "/api-keys",
    response_model=ListResponse[ApiKeyRead],
    tags=["api-keys"],
)
def list_api_keys(
    person_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = auth_service.api_keys.list(
        db, person_id, is_active, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.patch(
    "/api-keys/{key_id}",
    response_model=ApiKeyRead,
    tags=["api-keys"],
)
def update_api_key(key_id: str, payload: ApiKeyUpdate, db: Session = Depends(get_db)):
    return auth_service.api_keys.update(db, key_id, payload)


@router.delete(
    "/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["api-keys"],
)
def delete_api_key(key_id: str, db: Session = Depends(get_db)):
    auth_service.api_keys.revoke(db, key_id)
