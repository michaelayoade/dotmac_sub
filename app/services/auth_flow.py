from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, Response, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models.auth import (
    AuthProvider,
    MFAMethod,
    MFAMethodType,
    Session as AuthSession,
    SessionStatus,
    UserCredential,
)
from app.services.common import coerce_uuid
from app.services.response import ListResponseMixin
from app.models.rbac import Permission, PersonRole, Role, RolePermission
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.person import Person
from app.services.secrets import resolve_secret
from app.services import radius_auth as radius_auth_service
from app.schemas.auth_flow import LoginResponse, LogoutResponse, TokenResponse

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
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
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
        return setting.value_text
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
    person_id: str,
    session_id: str,
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
) -> str:
    now = _now()
    payload = {
        "sub": person_id,
        "session_id": session_id,
        "typ": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=_access_ttl_minutes(db))).timestamp()),
    }
    if roles:
        payload["roles"] = roles
    if permissions:
        payload["scopes"] = permissions
    return jwt.encode(payload, _jwt_secret(db), algorithm=_jwt_algorithm(db))


def _issue_mfa_token(db: Session | None, person_id: str) -> str:
    now = _now()
    payload = {
        "sub": person_id,
        "typ": "mfa",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(db), algorithm=_jwt_algorithm(db))


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


def _issue_password_reset_token(db: Session | None, person_id: str, email: str) -> str:
    now = _now()
    payload = {
        "sub": person_id,
        "email": email,
        "typ": "password_reset",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=_password_reset_ttl_minutes(db))).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(db), algorithm=_jwt_algorithm(db))


def _decode_password_reset_token(db: Session | None, token: str) -> dict:
    return _decode_jwt(db, token, "password_reset")


def _decode_jwt(db: Session | None, token: str, expected_type: str) -> dict:
    try:
        payload = jwt.decode(token, _jwt_secret(db), algorithms=[_jwt_algorithm(db)])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    if payload.get("typ") != expected_type:
        raise HTTPException(status_code=401, detail="Invalid token type")
    return payload


def decode_access_token(db: Session | None, token: str) -> dict:
    return _decode_jwt(db, token, "access")


def _person_or_404(db: Session, person_id: str) -> Person:
    person = db.get(Person, coerce_uuid(person_id))
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")
    return person


def _load_rbac_claims(db: Session, person_id: str):
    if db is None:
        return [], []
    person_uuid = coerce_uuid(person_id)
    roles = (
        db.query(Role)
        .join(PersonRole, PersonRole.role_id == Role.id)
        .filter(PersonRole.person_id == person_uuid)
        .filter(Role.is_active.is_(True))
        .all()
    )
    permissions = (
        db.query(Permission)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, RolePermission.role_id == Role.id)
        .join(PersonRole, PersonRole.role_id == Role.id)
        .filter(PersonRole.person_id == person_uuid)
        .filter(Role.is_active.is_(True))
        .filter(Permission.is_active.is_(True))
        .all()
    )
    role_names = [role.name for role in roles]
    permission_keys = list({perm.key for perm in permissions})
    return role_names, permission_keys


def _primary_totp_method(db: Session, person_id: str) -> MFAMethod | None:
    return (
        db.query(MFAMethod)
        .filter(MFAMethod.person_id == coerce_uuid(person_id))
        .filter(MFAMethod.method_type == MFAMethodType.totp)
        .filter(MFAMethod.is_active.is_(True))
        .filter(MFAMethod.enabled.is_(True))
        .filter(MFAMethod.is_primary.is_(True))
        .first()
    )


def _encrypt_secret(db: Session | None, secret: str) -> str:
    return _fernet(db).encrypt(secret.encode("utf-8")).decode("utf-8")


def _decrypt_secret(db: Session | None, secret: str) -> str:
    try:
        return _fernet(db).decrypt(secret.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise HTTPException(status_code=500, detail="Invalid MFA secret") from exc


def hash_password(password: str) -> str:
    return PASSWORD_CONTEXT.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    return PASSWORD_CONTEXT.verify(password, password_hash)


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
            credential = (
                db.query(UserCredential)
                .filter(UserCredential.username == username)
                .filter(UserCredential.provider == AuthProvider.radius)
                .filter(UserCredential.is_active.is_(True))
                .first()
            )
            if not credential:
                raise HTTPException(status_code=401, detail="Invalid credentials")
            radius_auth_service.authenticate(
                db, username, password, str(credential.radius_server_id) if credential.radius_server_id else None
            )
        elif resolved_provider == AuthProvider.local:
            credential = (
                db.query(UserCredential)
                .filter(UserCredential.username == username)
                .filter(UserCredential.provider == AuthProvider.local)
                .filter(UserCredential.is_active.is_(True))
                .first()
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

        if _primary_totp_method(db, str(credential.person_id)):
            return {
                "mfa_required": True,
                "mfa_token": _issue_mfa_token(db, str(credential.person_id)),
            }

        return AuthFlow._issue_tokens(db, credential.person_id, request)

    @staticmethod
    def mfa_setup(db: Session, person_id: str, label: str | None):
        person = _person_or_404(db, person_id)
        username = person.email
        credential = (
            db.query(UserCredential)
            .filter(UserCredential.person_id == person.id)
            .filter(UserCredential.provider == AuthProvider.local)
            .first()
        )
        if credential and credential.username:
            username = credential.username

        secret = pyotp.random_base32()
        encrypted = _encrypt_secret(db, secret)
        method = MFAMethod(
            person_id=person.id,
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
    def mfa_confirm(db: Session, method_id: str, code: str, person_id: str):
        method = db.get(MFAMethod, coerce_uuid(method_id))
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")
        if str(method.person_id) != str(person_id):
            raise HTTPException(status_code=403, detail="MFA method not allowed")
        if method.method_type != MFAMethodType.totp:
            raise HTTPException(status_code=400, detail="Unsupported MFA method")

        secret = _decrypt_secret(db, method.secret or "")
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=0):
            raise HTTPException(status_code=401, detail="Invalid MFA code")

        db.query(MFAMethod).filter(
            MFAMethod.person_id == method.person_id,
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
        person_id = payload.get("sub")
        if not person_id:
            raise HTTPException(status_code=401, detail="Invalid MFA token")

        method = _primary_totp_method(db, str(person_id))
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")

        secret = _decrypt_secret(db, method.secret or "")
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=0):
            raise HTTPException(status_code=401, detail="Invalid MFA code")

        method.last_used_at = _now()
        db.commit()
        return AuthFlow._issue_tokens(db, person_id, request)

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

        roles, permissions = _load_rbac_claims(db, str(session.person_id))
        access_token = _issue_access_token(
            db, str(session.person_id), str(session.id), roles, permissions
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
    def _issue_tokens(db: Session, person_id: str, request: Request):
        person_uuid = coerce_uuid(person_id)
        refresh_token = secrets.token_urlsafe(48)
        now = _now()
        expires_at = now + timedelta(days=_refresh_ttl_days(db))
        session = AuthSession(
            person_id=person_uuid,
            status=SessionStatus.active,
            token_hash=_hash_token(refresh_token),
            ip_address=request.client.host if request.client else None,
            user_agent=_truncate_user_agent(request.headers.get("user-agent")),
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        roles, permissions = _load_rbac_claims(db, str(person_uuid))
        access_token = _issue_access_token(db, str(person_uuid), str(session.id), roles, permissions)
        return {"access_token": access_token, "refresh_token": refresh_token}


auth_flow = AuthFlow()


def request_password_reset(db: Session, email: str) -> dict | None:
    """
    Request a password reset for the given email.
    Returns dict with token and person info if successful, None if email not found.
    Does not raise an error if email doesn't exist (security best practice).
    """
    person = db.query(Person).filter(Person.email == email).first()
    if not person:
        return None

    credential = (
        db.query(UserCredential)
        .filter(UserCredential.person_id == person.id)
        .filter(UserCredential.is_active.is_(True))
        .first()
    )
    if not credential:
        return None

    token = _issue_password_reset_token(db, str(person.id), email)
    return {
        "token": token,
        "email": email,
        "person_name": person.display_name or person.first_name,
    }


def reset_password(db: Session, token: str, new_password: str) -> datetime:
    """
    Reset password using a valid reset token.
    Returns the timestamp when password was reset.
    """
    payload = _decode_password_reset_token(db, token)
    person_id = payload.get("sub")
    email = payload.get("email")

    if not person_id or not email:
        raise HTTPException(status_code=401, detail="Invalid reset token")

    person = db.get(Person, coerce_uuid(person_id))
    if not person or person.email != email:
        raise HTTPException(status_code=401, detail="Invalid reset token")

    credential = (
        db.query(UserCredential)
        .filter(UserCredential.person_id == person.id)
        .filter(UserCredential.is_active.is_(True))
        .first()
    )
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
