from __future__ import annotations

import hashlib
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, Response, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import func, select as sa_select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.auth import (
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
from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SubscriberRole,
    SystemUserPermission,
    SystemUserRole,
)
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.schemas.auth_flow import LoginResponse, LogoutResponse, TokenResponse
from app.services import radius_auth as radius_auth_service
from app.services.common import coerce_uuid
from app.services.response import ListResponseMixin
from app.services.secrets import resolve_secret

PASSWORD_CONTEXT = CryptContext(
    schemes=["pbkdf2_sha256", "bcrypt"],
    default="pbkdf2_sha256",
    deprecated="auto",
)


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return value


def _env_int(name: str) -> int | None:
    raw = _env_value(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _truncate_user_agent(value: str | None, max_len: int = 512) -> str | None:
    if not value:
        return value
    if len(value) <= max_len:
        return value
    return value[:max_len]


def _setting_value(db: Session | None, key: str) -> str | None:
    if db is None:
        return None
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.auth)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        return None
    if setting.value_text:
        return cast(str, setting.value_text)
    if setting.value_json is not None:
        return str(setting.value_json)
    return None


def _jwt_secret(db: Session | None) -> str:
    secret = _env_value("JWT_SECRET") or _setting_value(db, "jwt_secret")
    secret = resolve_secret(secret)
    if not secret:
        raise HTTPException(status_code=500, detail="JWT secret not configured")
    return secret


def _jwt_algorithm(db: Session | None) -> str:
    return _env_value("JWT_ALGORITHM") or _setting_value(db, "jwt_algorithm") or "HS256"


def _access_ttl_minutes(db: Session | None) -> int:
    env_value = _env_int("JWT_ACCESS_TTL_MINUTES")
    if env_value is not None:
        return env_value
    value = _setting_value(db, "jwt_access_ttl_minutes")
    if value is not None:
        try:
            return int(value)
        except ValueError:
            return 15
    return 15


def _refresh_ttl_days(db: Session | None) -> int:
    env_value = _env_int("JWT_REFRESH_TTL_DAYS")
    if env_value is not None:
        return env_value
    value = _setting_value(db, "jwt_refresh_ttl_days")
    if value is not None:
        try:
            return int(value)
        except ValueError:
            return 30
    return 30


def _totp_issuer(db: Session | None) -> str:
    return _env_value("TOTP_ISSUER") or _setting_value(db, "totp_issuer") or "dotmac_sm"


def _refresh_cookie_name(db: Session | None) -> str:
    return (
        _env_value("REFRESH_COOKIE_NAME")
        or _setting_value(db, "refresh_cookie_name")
        or "refresh_token"
    )


def _refresh_cookie_secure(db: Session | None) -> bool:
    env_value = _env_value("REFRESH_COOKIE_SECURE")
    if env_value is not None:
        return env_value.lower() in {"1", "true", "yes", "on"}
    value = _setting_value(db, "refresh_cookie_secure")
    if value is not None:
        return str(value).lower() in {"1", "true", "yes", "on"}
    return False


def _refresh_cookie_samesite(db: Session | None) -> str:
    return (
        _env_value("REFRESH_COOKIE_SAMESITE")
        or _setting_value(db, "refresh_cookie_samesite")
        or "lax"
    )


def _refresh_cookie_domain(db: Session | None) -> str | None:
    return _env_value("REFRESH_COOKIE_DOMAIN") or _setting_value(
        db, "refresh_cookie_domain"
    )


def _refresh_cookie_path(db: Session | None) -> str:
    return (
        _env_value("REFRESH_COOKIE_PATH")
        or _setting_value(db, "refresh_cookie_path")
        or "/auth"
    )


def _mfa_key(db: Session | None) -> bytes:
    key = _env_value("TOTP_ENCRYPTION_KEY") or _setting_value(db, "totp_encryption_key")
    key = resolve_secret(key)
    if not key:
        raise HTTPException(status_code=500, detail="TOTP encryption key not configured")
    return key.encode()


def _fernet(db: Session | None) -> Fernet:
    try:
        return Fernet(_mfa_key(db))
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Invalid TOTP encryption key") from exc


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_session_token(token: str) -> str:
    return _hash_token(token)


def _issue_access_token(
    db: Session | None,
    principal_id: str,
    principal_type_or_session_id: str,
    session_id: str | None = None,
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
) -> str:
    # Backward compatibility: older callers passed (db, principal_id, session_id, ...)
    # and implicitly targeted subscriber principals.
    if session_id is None:
        principal_type = "subscriber"
        resolved_session_id = principal_type_or_session_id
    else:
        principal_type = principal_type_or_session_id
        resolved_session_id = session_id

    now = _now()
    payload = {
        "sub": principal_id,
        "principal_id": principal_id,
        "principal_type": principal_type,
        "session_id": resolved_session_id,
        "typ": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=_access_ttl_minutes(db))).timestamp()),
    }
    if roles:
        payload["roles"] = roles
    if permissions:
        payload["scopes"] = permissions
    return cast(str, jwt.encode(payload, _jwt_secret(db), algorithm=_jwt_algorithm(db)))


def _issue_mfa_token(
    db: Session | None,
    principal_id: str,
    principal_type: str = "subscriber",
) -> str:
    now = _now()
    payload = {
        "sub": principal_id,
        "principal_id": principal_id,
        "principal_type": principal_type,
        "typ": "mfa",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    return cast(str, jwt.encode(payload, _jwt_secret(db), algorithm=_jwt_algorithm(db)))


def _password_reset_ttl_minutes(db: Session | None) -> int:
    env_value = _env_int("PASSWORD_RESET_TTL_MINUTES")
    if env_value is not None:
        return env_value
    value = _setting_value(db, "password_reset_ttl_minutes")
    if value is not None:
        try:
            return int(value)
        except ValueError:
            return 60
    return 60


def _issue_password_reset_token(
    db: Session | None,
    principal_id: str,
    principal_type_or_email: str,
    email: str | None = None,
) -> str:
    # Backward compatibility: older callers passed (db, principal_id, email)
    # and implicitly targeted subscriber principals.
    if email is None:
        principal_type = "subscriber"
        resolved_email = principal_type_or_email
    else:
        principal_type = principal_type_or_email
        resolved_email = email

    now = _now()
    payload = {
        "sub": principal_id,
        "principal_id": principal_id,
        "principal_type": principal_type,
        "email": resolved_email,
        "typ": "password_reset",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=_password_reset_ttl_minutes(db))).timestamp()),
    }
    return cast(str, jwt.encode(payload, _jwt_secret(db), algorithm=_jwt_algorithm(db)))


def _decode_password_reset_token(db: Session | None, token: str) -> dict:
    return _decode_jwt(db, token, "password_reset")


def _decode_jwt(db: Session | None, token: str, expected_type: str) -> dict:
    try:
        payload = cast(
            dict[Any, Any],
            jwt.decode(token, _jwt_secret(db), algorithms=[_jwt_algorithm(db)]),
        )
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    if payload.get("typ") != expected_type:
        raise HTTPException(status_code=401, detail="Invalid token type")
    return payload


def decode_access_token(db: Session | None, token: str) -> dict:
    return _decode_jwt(db, token, "access")


def _subscriber_or_404(db: Session, subscriber_id: str) -> Subscriber:
    subscriber = cast(Subscriber | None, db.get(Subscriber, coerce_uuid(subscriber_id)))
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    return subscriber


def _person_or_404(db: Session, person_id: str) -> Subscriber:
    """Backwards-compatible helper: people are subscribers in this codebase."""
    return _subscriber_or_404(db, person_id)


def _load_rbac_claims(
    db: Session,
    principal_type_or_principal_id: str,
    principal_id: str | None = None,
):
    if db is None:
        return [], []
    if principal_id is None:
        principal_type = "subscriber"
        resolved_principal_id = principal_type_or_principal_id
    else:
        principal_type = principal_type_or_principal_id
        resolved_principal_id = principal_id
    principal_uuid = coerce_uuid(resolved_principal_id)
    if principal_type == "system_user":
        roles = (
            db.query(Role)
            .join(SystemUserRole, SystemUserRole.role_id == Role.id)
            .filter(SystemUserRole.system_user_id == principal_uuid)
            .filter(Role.is_active.is_(True))
            .all()
        )
        permissions = (
            db.query(Permission)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, RolePermission.role_id == Role.id)
            .join(SystemUserRole, SystemUserRole.role_id == Role.id)
            .filter(SystemUserRole.system_user_id == principal_uuid)
            .filter(Role.is_active.is_(True))
            .filter(Permission.is_active.is_(True))
            .all()
        )
        direct_permissions = (
            db.query(Permission)
            .join(SystemUserPermission, SystemUserPermission.permission_id == Permission.id)
            .filter(SystemUserPermission.system_user_id == principal_uuid)
            .filter(Permission.is_active.is_(True))
            .all()
        )
    else:
        roles = (
            db.query(Role)
            .join(SubscriberRole, SubscriberRole.role_id == Role.id)
            .filter(SubscriberRole.subscriber_id == principal_uuid)
            .filter(Role.is_active.is_(True))
            .all()
        )
        permissions = (
            db.query(Permission)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, RolePermission.role_id == Role.id)
            .join(SubscriberRole, SubscriberRole.role_id == Role.id)
            .filter(SubscriberRole.subscriber_id == principal_uuid)
            .filter(Role.is_active.is_(True))
            .filter(Permission.is_active.is_(True))
            .all()
        )
        direct_permissions = []
    role_names = [role.name for role in roles]
    permission_keys = list({perm.key for perm in [*permissions, *direct_permissions]})
    return role_names, permission_keys


def _resolve_login_credential(
    db: Session,
    *,
    provider: AuthProvider,
    identifier: str,
) -> UserCredential | None:
    """Resolve active credential using either username or subscriber email."""
    normalized_identifier = identifier.strip()
    if not normalized_identifier:
        return None

    return cast(
        UserCredential | None,
        db.query(UserCredential)
        .outerjoin(Subscriber, Subscriber.id == UserCredential.subscriber_id)
        .outerjoin(SystemUser, SystemUser.id == UserCredential.system_user_id)
        .filter(UserCredential.provider == provider)
        .filter(UserCredential.is_active.is_(True))
        .filter(
            (UserCredential.username == normalized_identifier)
            | (func.lower(Subscriber.email) == normalized_identifier.lower())
            | (func.lower(SystemUser.email) == normalized_identifier.lower())
        )
        .order_by(UserCredential.created_at.desc())
        .first(),
    )


def _principal_for_credential(db: Session, credential: UserCredential) -> tuple[str, str, object | None]:
    if credential.system_user_id:
        return "system_user", str(credential.system_user_id), db.get(SystemUser, credential.system_user_id)
    if credential.subscriber_id:
        return "subscriber", str(credential.subscriber_id), db.get(Subscriber, credential.subscriber_id)
    return "subscriber", "", None


def _primary_totp_method(db: Session, principal_type: str, principal_id: str) -> MFAMethod | None:
    query = db.query(MFAMethod).filter(MFAMethod.method_type == MFAMethodType.totp)
    if principal_type == "system_user":
        query = query.filter(MFAMethod.system_user_id == coerce_uuid(principal_id))
    else:
        query = query.filter(MFAMethod.subscriber_id == coerce_uuid(principal_id))
    return cast(
        MFAMethod | None,
        query.filter(MFAMethod.is_active.is_(True))
        .filter(MFAMethod.enabled.is_(True))
        .filter(MFAMethod.is_primary.is_(True))
        .first(),
    )


def _encrypt_secret(db: Session | None, secret: str) -> str:
    return _fernet(db).encrypt(secret.encode("utf-8")).decode("utf-8")


def _decrypt_secret(db: Session | None, secret: str) -> str:
    try:
        return _fernet(db).decrypt(secret.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise HTTPException(status_code=500, detail="Invalid MFA secret") from exc


def hash_password(password: str) -> str:
    return cast(str, PASSWORD_CONTEXT.hash(password))


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    return cast(bool, PASSWORD_CONTEXT.verify(password, password_hash))


class AuthFlow(ListResponseMixin):
    @staticmethod
    def _response_with_refresh_cookie(
        db: Session | None,
        payload: dict,
        model_cls,
        status_code: int = status.HTTP_200_OK,
    ) -> Response:
        settings = AuthFlow.refresh_cookie_settings(db)
        body_payload = {**payload, "refresh_token": None}
        body_content = model_cls(**body_payload).model_dump_json()
        response = Response(
            content=body_content,
            status_code=status_code,
            media_type="application/json",
        )
        response.set_cookie(
            key=settings["key"],
            value=payload["refresh_token"],
            httponly=settings["httponly"],
            secure=settings["secure"],
            samesite=settings["samesite"],
            domain=settings["domain"],
            path=settings["path"],
            max_age=settings["max_age"],
        )
        return response

    @staticmethod
    def _response_clear_refresh_cookie(
        db: Session | None,
        payload: dict,
        model_cls,
        status_code: int = status.HTTP_200_OK,
    ) -> Response:
        settings = AuthFlow.refresh_cookie_settings(db)
        body_content = model_cls(**payload).model_dump_json()
        response = Response(
            content=body_content,
            status_code=status_code,
            media_type="application/json",
        )
        response.delete_cookie(
            key=settings["key"],
            domain=settings["domain"],
            path=settings["path"],
        )
        return response

    @staticmethod
    def login_response(
        db: Session, username: str, password: str, request: Request, provider: str | None
    ):
        result = AuthFlow.login(db, username, password, request, provider)
        if result.get("refresh_token"):
            return AuthFlow._response_with_refresh_cookie(
                db, result, LoginResponse, status.HTTP_200_OK
            )
        return result

    @staticmethod
    def login(
        db: Session, username: str, password: str, request: Request, provider: str | None
    ):
        if isinstance(provider, AuthProvider):
            provider_value = provider.value
        else:
            provider_value = provider or AuthProvider.local.value
        try:
            resolved_provider = AuthProvider(provider_value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid auth provider") from exc
        if resolved_provider == AuthProvider.radius:
            credential = _resolve_login_credential(
                db,
                provider=AuthProvider.radius,
                identifier=username,
            )
            if not credential:
                raise HTTPException(status_code=401, detail="Invalid credentials")
            radius_auth_service.authenticate(
                db,
                str(credential.username or username),
                password,
                str(credential.radius_server_id) if credential.radius_server_id else None,
            )
        elif resolved_provider == AuthProvider.local:
            credential = _resolve_login_credential(
                db,
                provider=AuthProvider.local,
                identifier=username,
            )
            if not credential:
                raise HTTPException(status_code=401, detail="Invalid credentials")
            if not verify_password(password, credential.password_hash):
                now = _now()
                credential.failed_login_attempts += 1
                if credential.failed_login_attempts >= 5:
                    credential.locked_until = now + timedelta(minutes=15)
                db.commit()
                raise HTTPException(status_code=401, detail="Invalid credentials")
        else:
            raise HTTPException(status_code=400, detail="Unsupported auth provider")

        now = _now()
        locked_until = _as_utc(credential.locked_until)
        if locked_until and locked_until > now:
            raise HTTPException(status_code=403, detail="Account locked")

        if credential.must_change_password:
            raise HTTPException(
                status_code=428,
                detail={
                    "code": "PASSWORD_RESET_REQUIRED",
                    "message": "Password reset required",
                },
            )

        credential.failed_login_attempts = 0
        credential.locked_until = None
        credential.last_login_at = now
        db.commit()

        principal_type, principal_id, principal = _principal_for_credential(db, credential)
        if not principal or not getattr(principal, "is_active", False):
            raise HTTPException(status_code=403, detail="Account disabled")
        if _primary_totp_method(db, principal_type, principal_id):
            return {
                "mfa_required": True,
                "mfa_token": _issue_mfa_token(db, principal_id, principal_type),
            }

        return AuthFlow._issue_tokens(db, principal_type, principal_id, request)

    @staticmethod
    def mfa_setup(db: Session, subscriber_id: str, label: str | None):
        subscriber = _subscriber_or_404(db, subscriber_id)
        username = subscriber.email
        credential = (
            db.query(UserCredential)
            .filter(UserCredential.subscriber_id == subscriber.id)
            .filter(UserCredential.provider == AuthProvider.local)
            .first()
        )
        if credential and credential.username:
            username = credential.username

        secret = pyotp.random_base32()
        encrypted = _encrypt_secret(db, secret)
        method = MFAMethod(
            subscriber_id=subscriber.id,
            method_type=MFAMethodType.totp,
            label=label,
            secret=encrypted,
            enabled=False,
            is_primary=False,
        )
        db.add(method)
        db.commit()
        db.refresh(method)

        totp = pyotp.TOTP(secret)
        otpauth_uri = totp.provisioning_uri(
            name=username, issuer_name=_totp_issuer(db)
        )
        return {"method_id": method.id, "secret": secret, "otpauth_uri": otpauth_uri}

    @staticmethod
    def mfa_confirm(db: Session, method_id: str, code: str, subscriber_id: str):
        method = db.get(MFAMethod, coerce_uuid(method_id))
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")
        if str(method.subscriber_id) != str(subscriber_id):
            raise HTTPException(status_code=403, detail="MFA method not allowed")
        if method.method_type != MFAMethodType.totp:
            raise HTTPException(status_code=400, detail="Unsupported MFA method")

        secret = _decrypt_secret(db, method.secret or "")
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=0):
            raise HTTPException(status_code=401, detail="Invalid MFA code")

        db.query(MFAMethod).filter(
            MFAMethod.subscriber_id == method.subscriber_id,
            MFAMethod.id != method.id,
            MFAMethod.is_primary.is_(True),
        ).update({"is_primary": False})

        method.enabled = True
        method.is_primary = True
        method.is_active = True
        method.verified_at = _now()
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
    def mfa_verify(db: Session, mfa_token: str, code: str, request: Request):
        payload = _decode_jwt(db, mfa_token, "mfa")
        principal_id = payload.get("principal_id") or payload.get("sub")
        principal_type = payload.get("principal_type") or "subscriber"
        if not principal_id:
            raise HTTPException(status_code=401, detail="Invalid MFA token")

        method = _primary_totp_method(db, principal_type, str(principal_id))
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")

        secret = _decrypt_secret(db, method.secret or "")
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=0):
            raise HTTPException(status_code=401, detail="Invalid MFA code")

        method.last_used_at = _now()
        db.commit()
        return AuthFlow._issue_tokens(db, principal_type, str(principal_id), request)

    @staticmethod
    def mfa_verify_response(db: Session, mfa_token: str, code: str, request: Request):
        result = AuthFlow.mfa_verify(db, mfa_token, code, request)
        return AuthFlow._response_with_refresh_cookie(
            db, result, TokenResponse, status.HTTP_200_OK
        )

    @staticmethod
    def refresh(db: Session, refresh_token: str, request: Request):
        token_hash = _hash_token(refresh_token)
        session = (
            db.query(AuthSession)
            .filter(AuthSession.token_hash == token_hash)
            .filter(AuthSession.status == SessionStatus.active)
            .filter(AuthSession.revoked_at.is_(None))
            .first()
        )
        if not session:
            reused = (
                db.query(AuthSession)
                .filter(AuthSession.previous_token_hash == token_hash)
                .filter(AuthSession.status == SessionStatus.active)
                .filter(AuthSession.revoked_at.is_(None))
                .first()
            )
            if reused:
                reused.status = SessionStatus.revoked
                reused.revoked_at = _now()
                db.commit()
                raise HTTPException(
                    status_code=401,
                    detail="Refresh token reuse detected",
                )
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        expires_at = _as_utc(session.expires_at)
        if expires_at and expires_at <= _now():
            session.status = SessionStatus.expired
            db.commit()
            raise HTTPException(status_code=401, detail="Refresh token expired")

        new_refresh = secrets.token_urlsafe(48)
        session.previous_token_hash = session.token_hash
        session.token_hash = _hash_token(new_refresh)
        session.token_rotated_at = _now()
        session.last_seen_at = _now()
        if request.client:
            session.ip_address = request.client.host
        session.user_agent = _truncate_user_agent(request.headers.get("user-agent"))
        db.commit()

        principal_type = "system_user" if session.system_user_id else "subscriber"
        principal_id = str(session.system_user_id or session.subscriber_id)
        roles, permissions = _load_rbac_claims(db, principal_type, principal_id)
        access_token = _issue_access_token(
            db, principal_id, principal_type, str(session.id), roles, permissions
        )
        return {"access_token": access_token, "refresh_token": new_refresh}

    @staticmethod
    def refresh_response(db: Session, refresh_token: str | None, request: Request):
        resolved = AuthFlow.resolve_refresh_token(request, refresh_token, db)
        if not resolved:
            raise HTTPException(status_code=401, detail="Missing refresh token")
        result = AuthFlow.refresh(db, resolved, request)
        return AuthFlow._response_with_refresh_cookie(
            db, result, TokenResponse, status.HTTP_200_OK
        )

    @staticmethod
    def logout(db: Session, refresh_token: str):
        token_hash = _hash_token(refresh_token)
        session = (
            db.query(AuthSession)
            .filter(AuthSession.token_hash == token_hash)
            .filter(AuthSession.revoked_at.is_(None))
            .first()
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        session.status = SessionStatus.revoked
        session.revoked_at = _now()
        db.commit()
        return {"revoked_at": session.revoked_at}

    @staticmethod
    def logout_response(db: Session, refresh_token: str | None, request: Request):
        resolved = AuthFlow.resolve_refresh_token(request, refresh_token, db)
        if not resolved:
            raise HTTPException(status_code=404, detail="Session not found")
        result = AuthFlow.logout(db, resolved)
        return AuthFlow._response_clear_refresh_cookie(
            db, result, LogoutResponse, status.HTTP_200_OK
        )

    @staticmethod
    def resolve_refresh_token(request: Request, refresh_token: str | None, db: Session | None = None):
        settings = AuthFlow.refresh_cookie_settings(db)
        return refresh_token or request.cookies.get(settings["key"])

    @staticmethod
    def refresh_cookie_settings(db: Session | None = None):
        return {
            "key": _refresh_cookie_name(db),
            "httponly": True,
            "secure": _refresh_cookie_secure(db),
            "samesite": _refresh_cookie_samesite(db),
            "domain": _refresh_cookie_domain(db),
            "path": _refresh_cookie_path(db),
            "max_age": _refresh_ttl_days(db) * 24 * 60 * 60,
        }

    @staticmethod
    def _issue_tokens(
        db: Session,
        principal_type_or_principal_id: str,
        principal_id_or_request: str | Request,
        request: Request | None = None,
    ):
        # Backward compatibility: older callers passed (db, principal_id, request)
        # and implicitly targeted subscriber principals.
        if request is None:
            principal_type = "subscriber"
            principal_id = principal_type_or_principal_id
            active_request = cast(Request, principal_id_or_request)
        else:
            principal_type = principal_type_or_principal_id
            principal_id = cast(str, principal_id_or_request)
            active_request = request

        principal_uuid = coerce_uuid(principal_id)
        refresh_token = secrets.token_urlsafe(48)
        now = _now()
        expires_at = now + timedelta(days=_refresh_ttl_days(db))
        if principal_type == "system_user":
            session = AuthSession(
                system_user_id=principal_uuid,
                status=SessionStatus.active,
                token_hash=_hash_token(refresh_token),
                ip_address=active_request.client.host if active_request.client else None,
                user_agent=_truncate_user_agent(active_request.headers.get("user-agent")),
                created_at=now,
                last_seen_at=now,
                expires_at=expires_at,
            )
        else:
            session = AuthSession(
                subscriber_id=principal_uuid,
                status=SessionStatus.active,
                token_hash=_hash_token(refresh_token),
                ip_address=active_request.client.host if active_request.client else None,
                user_agent=_truncate_user_agent(active_request.headers.get("user-agent")),
                created_at=now,
                last_seen_at=now,
                expires_at=expires_at,
            )
        db.add(session)
        db.commit()
        db.refresh(session)
        roles, permissions = _load_rbac_claims(db, principal_type, str(principal_uuid))
        access_token = _issue_access_token(
            db, str(principal_uuid), principal_type, str(session.id), roles, permissions
        )
        return {"access_token": access_token, "refresh_token": refresh_token}


auth_flow = AuthFlow()


def change_password(
    db: Session,
    subscriber_id: str,
    current_password: str,
    new_password: str,
) -> datetime:
    """
    Change a user's password after verifying the current password.
    Returns the timestamp when the password was changed.
    """
    principal_uuid = coerce_uuid(subscriber_id)
    stmt = (
        sa_select(UserCredential)
        .where(
            (UserCredential.subscriber_id == principal_uuid)
            | (UserCredential.system_user_id == principal_uuid)
        )
        .where(UserCredential.is_active.is_(True))
    )
    credential = db.scalars(stmt).first()

    if not credential:
        raise HTTPException(status_code=404, detail="No credentials found")

    if not verify_password(current_password, credential.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    if current_password == new_password:
        raise HTTPException(status_code=400, detail="New password must be different")

    now = _now()
    credential.password_hash = hash_password(new_password)
    credential.password_updated_at = now
    credential.must_change_password = False
    db.flush()

    return now


def forgot_password_flow(db: Session, email: str) -> None:
    """
    Handle the forgot-password flow: generate a reset token and send the email.
    Always completes without error to prevent email enumeration.
    """
    from app.services.email import send_password_reset_email

    result = request_password_reset(db, email)
    if result:
        send_password_reset_email(
            db=db,
            to_email=result["email"],
            reset_token=result["token"],
            person_name=result.get("subscriber_name"),
        )


def request_password_reset(db: Session, email: str) -> dict | None:
    """
    Request a password reset for the given email.
    Returns dict with token and person info if successful, None if email not found.
    Does not raise an error if email doesn't exist (security best practice).
    """
    normalized_email = email.strip().lower()
    subscriber = (
        db.query(Subscriber).filter(func.lower(Subscriber.email) == normalized_email).first()
    )
    if subscriber:
        credential = (
            db.query(UserCredential)
            .filter(UserCredential.subscriber_id == subscriber.id)
            .filter(UserCredential.is_active.is_(True))
            .first()
        )
        if credential:
            token = _issue_password_reset_token(
                db, str(subscriber.id), "subscriber", subscriber.email
            )
            return {
                "token": token,
                "email": subscriber.email,
                "subscriber_name": subscriber.display_name or subscriber.first_name,
            }

    system_user = (
        db.query(SystemUser).filter(func.lower(SystemUser.email) == normalized_email).first()
    )
    if not system_user:
        return None
    credential = (
        db.query(UserCredential)
        .filter(UserCredential.system_user_id == system_user.id)
        .filter(UserCredential.is_active.is_(True))
        .first()
    )
    if not credential:
        return None
    token = _issue_password_reset_token(
        db, str(system_user.id), "system_user", system_user.email
    )
    return {
        "token": token,
        "email": system_user.email,
        "subscriber_name": system_user.display_name or system_user.first_name,
    }


def reset_password(db: Session, token: str, new_password: str) -> datetime:
    """
    Reset password using a valid reset token.
    Returns the timestamp when password was reset.
    """
    payload = _decode_password_reset_token(db, token)
    principal_id = payload.get("principal_id") or payload.get("sub")
    principal_type = payload.get("principal_type") or "subscriber"
    email = payload.get("email")

    if not principal_id or not email:
        raise HTTPException(status_code=401, detail="Invalid reset token")

    if principal_type == "system_user":
        principal = db.get(SystemUser, coerce_uuid(principal_id))
        credential_query = db.query(UserCredential).filter(
            UserCredential.system_user_id == coerce_uuid(principal_id)
        )
    else:
        principal = db.get(Subscriber, coerce_uuid(principal_id))
        credential_query = db.query(UserCredential).filter(
            UserCredential.subscriber_id == coerce_uuid(principal_id)
        )
    if not principal or principal.email != email:
        raise HTTPException(status_code=401, detail="Invalid reset token")

    credential = credential_query.filter(UserCredential.is_active.is_(True)).first()
    if not credential:
        raise HTTPException(status_code=404, detail="No credentials found")

    now = _now()
    credential.password_hash = hash_password(new_password)
    credential.password_updated_at = now
    credential.must_change_password = False
    credential.failed_login_attempts = 0
    credential.locked_until = None
    db.commit()

    return now


def validate_active_session(
    db: Session,
    session_id: str,
    principal_id: str,
) -> tuple[AuthSession, object, str] | None:
    """Validate that an active, non-expired session exists for the subscriber.

    Returns (session, subscriber) tuple if valid, else None.
    """
    now = _now()
    session = (
        db.query(AuthSession)
        .filter(AuthSession.id == session_id)
        .filter(AuthSession.status == SessionStatus.active)
        .filter(AuthSession.revoked_at.is_(None))
        .filter(AuthSession.expires_at > now)
        .first()
    )
    if not session:
        return None
    principal_type = "system_user" if session.system_user_id else "subscriber"
    active_id = str(session.system_user_id or session.subscriber_id)
    if active_id != str(principal_id):
        return None

    if principal_type == "system_user":
        principal = db.get(SystemUser, active_id)
    else:
        principal = db.get(Subscriber, active_id)
    if not principal:
        return None

    return session, principal, principal_type
