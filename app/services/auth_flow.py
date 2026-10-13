from __future__ import annotations

import logging
import secrets
import string
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Literal, cast
from uuid import UUID

import bcrypt
import pyotp
from cryptography.fernet import Fernet, InvalidToken

# Named `held_secret` at the import, not `get_secret`: `app.services.secrets`
# exports a `get_secret(path, field)` that TALKS TO OpenBao, and at a call site
# the two would be indistinguishable. This one is a dict lookup over material
# loaded once at boot — see `app/services/kernel_secret_source.py`.
from dotmac_kernel.secret_sources import get_secret as held_secret
from fastapi import HTTPException, Request, Response, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import func
from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.config import settings
from app.metrics import CREDENTIAL_FORMAT_REJECTIONS
from app.models.auth import (
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
from app.models.catalog import AccessCredential
from app.models.domain_settings import SettingDomain
from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SubscriberRole,
    SystemUserPermission,
    SystemUserRole,
)
from app.models.subscriber import ResellerUser, Subscriber, SubscriberStatus, UserType
from app.models.system_user import SystemUser
from app.request_meta import client_ip
from app.schemas.auth_flow import LoginResponse, LogoutResponse, TokenResponse
from app.services import (
    auth_cache,
    auth_session_refresh,
    auth_session_staging,
    auth_token_signing,
    customer_login_identity,
    password_authentication,
    staff_party_authentication,
    user_passwords,
)
from app.services import radius_auth as radius_auth_service
from app.services.auth_session_staging import StagedSession
from app.services.capability_recipient import resolve_capability_recipient
from app.services.common import coerce_uuid
from app.services.credential_crypto import decrypt_credential, encrypt_credential
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.response import ListResponseMixin
from app.services.secrets import resolve_secret
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

PASSWORD_CONTEXT = CryptContext(
    # "bcrypt" is deliberately NOT a passlib scheme: with bcrypt>=5 passlib's
    # backend self-test passes a >72-byte secret and raises ValueError, which
    # breaks every bcrypt verification. Legacy bcrypt hashes are verified by
    # `_verify_legacy_bcrypt` via `bcrypt.checkpw` instead (see verify_user_password).
    schemes=["pbkdf2_sha256", "sha512_crypt"],
    default="pbkdf2_sha256",
    deprecated="auto",
)

# bcrypt 5 `checkpw` accepts $2a$, $2b$, $2y$ (and, technically, $2x$). $2x$ is
# the known-buggy PHP crypt_blowfish variant and is deliberately unsupported.
_BCRYPT_VERIFY_PREFIXES = ("$2a$", "$2b$", "$2y$")
_BCRYPT_UNSUPPORTED_PREFIXES = ("$2x$",)
_BCRYPT_MAX_PASSWORD_BYTES = 72


class LoginAudience(str, Enum):
    """The portal a successful login is allowed to enter."""

    general = "general"
    admin = "admin"
    customer = "customer"


def is_admin_portal_principal(principal_type: str, principal: object | None) -> bool:
    """Whether a resolved principal may receive an admin-portal session."""

    return (
        principal_type == "system_user"
        and isinstance(principal, SystemUser)
        and principal.user_type is UserType.system_user
    )


def principal_refusal(
    principal_type: str,
    principal: object | None,
    audience: LoginAudience,
) -> str | None:
    """Why a verified principal may not receive a session, or ``None``.

    Pure, so phase A (login) and the in-transaction re-check (phase B) apply
    the identical rule: ``account_disabled`` | ``admin_required``.
    """

    if not principal or not getattr(principal, "is_active", False):
        return "account_disabled"
    if (
        principal_type == "subscriber"
        and isinstance(principal, Subscriber)
        and principal.status
        in {
            SubscriberStatus.disabled,
            SubscriberStatus.canceled,
        }
    ):
        return "account_disabled"
    if audience is LoginAudience.admin and not is_admin_portal_principal(
        principal_type, principal
    ):
        return "admin_required"
    if audience is LoginAudience.customer and (
        principal_type != "subscriber"
        or not isinstance(principal, Subscriber)
        or principal.user_type is not UserType.customer
    ):
        return "customer_required"
    return None


def _env_value(name: str) -> str | None:
    return auth_token_signing.env_value(name)


def _env_int(name: str) -> int | None:
    return auth_token_signing.env_int(name)


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def lockout_detail(
    prefix: str,
    *,
    locked_until: datetime | None = None,
    retry_after_seconds: int | None = None,
) -> str:
    remaining_seconds = 0
    normalized_until = _as_utc(locked_until)
    if normalized_until:
        remaining_seconds = max(int((normalized_until - _now()).total_seconds()), 0)
    elif retry_after_seconds is not None:
        remaining_seconds = max(int(retry_after_seconds), 0)

    if remaining_seconds <= 0:
        return f"{prefix}. Please try again later."
    minutes = max(1, (remaining_seconds + 59) // 60)
    unit = "minute" if minutes == 1 else "minutes"
    return f"{prefix}. Try again in {minutes} {unit}."


def duration_label(seconds: int) -> str:
    seconds = max(int(seconds), 1)
    for unit, unit_seconds in (
        ("day", 86400),
        ("hour", 3600),
        ("minute", 60),
    ):
        if seconds >= unit_seconds:
            value = max(1, (seconds + unit_seconds - 1) // unit_seconds)
            suffix = unit if value == 1 else f"{unit}s"
            return f"{value} {suffix}"
    return "1 minute"


def _truncate_user_agent(value: str | None, max_len: int = 512) -> str | None:
    return auth_session_refresh.normalize_user_agent(value, max_len)


def _clean_device_id(value: str | None, max_len: int = 64) -> str | None:
    """Normalise the client-supplied X-Device-Id (a per-install opaque id).
    Returns None when absent/blank so non-native callers keep the old
    one-session-per-login behaviour."""
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


def _setting_value(db: Session | None, key: str) -> str | None:
    return auth_token_signing.setting_value(db, key)


def _jwt_secret(db: Session | None) -> str:
    """The signing secret: the environment, else what was held at boot.

    The `auth/jwt_secret` SETTING is no longer consulted. It never held the
    secret — it held a `bao://` reference that this call dereferenced, so every
    token signed and every token verified could reach OpenBao over the network
    while handling a request. Starter ADR-0009 forbids exactly that: a secret
    is held, never dereferenced. The row's dereference also had no timeout of
    its own and no error path — a slow store made signing slow.

    Environment first, preserving the precedence this function already had.
    """

    try:
        return auth_token_signing.jwt_secret(db)
    except auth_token_signing.TokenSigningConfigurationError as exc:
        raise HTTPException(
            status_code=500, detail="JWT secret not configured"
        ) from exc


def _jwt_algorithm(db: Session | None) -> str:
    return auth_token_signing.jwt_algorithm(db)


def _access_ttl_minutes(db: Session | None) -> int:
    return auth_token_signing.access_ttl_minutes(db)


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


def _force_admin_mfa(db: Session | None) -> bool:
    value = _env_value("ADMIN_MFA_REQUIRED")
    if value is None:
        value = _setting_value(db, "admin_mfa_required")
    if value is None:
        value = _setting_value(db, "force_2fa")
    if value is None:
        value = resolve_value(db, SettingDomain.auth, "admin_mfa_required")
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _setting_int(
    db: Session | None, key: str, default: int, *, minimum: int = 1
) -> int:
    value = resolve_value(db, SettingDomain.auth, key)
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _refresh_cookie_name(db: Session | None) -> str:
    return (
        _env_value("REFRESH_COOKIE_NAME")
        or _setting_value(db, "refresh_cookie_name")
        or "refresh_token"
    )


def wants_refresh_in_body(request: Request | None) -> bool:
    """Native clients (mobile) can't read the httpOnly refresh cookie, so they
    opt into receiving the refresh token in the JSON body via this header and
    persist it in the platform secure store instead. Browser clients omit the
    header and keep the safer httpOnly-cookie behaviour.

    Public because it is ONE policy for the whole JSON API: the vendor
    authentication adapter (``app/api/vendor_auth.py``) has to answer the same
    question for the same field client, and a second copy of the header name
    would drift."""
    if request is None:
        return False
    return request.headers.get("x-auth-refresh-in-body", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _refresh_cookie_secure(db: Session | None) -> bool:
    env_value = _env_value("REFRESH_COOKIE_SECURE")
    if env_value is not None:
        return env_value.lower() in {"1", "true", "yes", "on"}
    value = _setting_value(db, "refresh_cookie_secure")
    if value is not None:
        return str(value).lower() in {"1", "true", "yes", "on"}
    # Secure-by-default: callers AND this with an HTTPS-request check, so plain
    # HTTP (local dev) still works while production never drops the flag over
    # TLS. Set REFRESH_COOKIE_SECURE=false to force-disable.
    return True


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
    """The TOTP-secret encryption key — held, not read from the database.

    Same reasoning as `_jwt_secret`: this key protects rows in the very
    database the reference used to live in, so it must not be stored there,
    and verifying one TOTP code must not depend on a network hop.
    """

    key = resolve_secret(_env_value("TOTP_ENCRYPTION_KEY")) or held_secret(
        "totp_encryption_key"
    )
    if not key:
        raise HTTPException(
            status_code=500, detail="TOTP encryption key not configured"
        )
    return key.encode()


def _fernet(db: Session | None) -> Fernet:
    try:
        return Fernet(_mfa_key(db))
    except ValueError as exc:
        raise HTTPException(
            status_code=500, detail="Invalid TOTP encryption key"
        ) from exc


def _hash_token(token: str) -> str:
    return auth_session_refresh.hash_refresh_token(token)


def principal_from_session(session: AuthSession) -> tuple[str, str]:
    """(principal_type, principal_id) for an AuthSession — subscriber, system_user,
    or reseller_user (Layer 3). Used by token issuance/refresh."""
    if session.system_user_id:
        return "system_user", str(session.system_user_id)
    if getattr(session, "reseller_user_id", None):
        return "reseller_user", str(session.reseller_user_id)
    return "subscriber", str(session.subscriber_id)


def auth_session_principal_filter(principal_type: str, principal_id):
    """AuthSession column filter for a principal — keeps session lookup/revocation
    correct for reseller_user principals (not just subscriber/system_user)."""
    if principal_type == "system_user":
        return AuthSession.system_user_id == principal_id
    if principal_type == "reseller_user":
        return AuthSession.reseller_user_id == principal_id
    return AuthSession.subscriber_id == principal_id


def credential_principal_filter(principal_type: str, principal_id):
    """UserCredential column filter for a principal (reseller_user-aware)."""
    if principal_type == "system_user":
        return UserCredential.system_user_id == principal_id
    if principal_type == "reseller_user":
        return UserCredential.reseller_user_id == principal_id
    return UserCredential.subscriber_id == principal_id


def _jwt_encode_token(payload: dict[str, Any], secret: str, algorithm: str) -> str:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"datetime\.datetime\.utcnow\(\) is deprecated.*",
            category=DeprecationWarning,
            module=r"jose\.jwt",
        )
        return cast(str, jwt.encode(payload, secret, algorithm=algorithm))


def _jwt_decode_token(token: str, secret: str, algorithm: str) -> dict[Any, Any]:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"datetime\.datetime\.utcnow\(\) is deprecated.*",
            category=DeprecationWarning,
            module=r"jose\.jwt",
        )
        return cast(dict[Any, Any], jwt.decode(token, secret, algorithms=[algorithm]))


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

    return auth_token_signing.issue_access_token(
        db,
        principal_id=principal_id,
        principal_type=principal_type,
        session_id=resolved_session_id,
        issued_at=_now(),
        roles=roles,
        permissions=permissions,
    )


def _encode_access_token(
    *,
    principal_id: str,
    principal_type: str,
    session_id: str,
    issued_at: datetime,
    ttl_minutes: int,
    secret: str,
    algorithm: str,
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
) -> str:
    """Encode an access token from immutable values, with no persistence reads."""

    return auth_token_signing.encode_access_token(
        principal_id=principal_id,
        principal_type=principal_type,
        session_id=session_id,
        issued_at=issued_at,
        ttl_minutes=ttl_minutes,
        secret=secret,
        algorithm=algorithm,
        roles=roles,
        permissions=permissions,
    )


def issue_impersonation_access_token(
    db: Session | None,
    subscriber_id: str,
    session_id: str,
    acting_subscriber_id: str,
    ttl_minutes: int = 15,
) -> str:
    """Short-lived customer-scoped token for reseller "view as customer".

    Carries ``imp``/``imp_by`` claims: the auth dependency enforces read-only
    (GET/HEAD/OPTIONS) for these tokens, and ``imp_by`` keeps the acting
    reseller attributable in request logs."""
    now = _now()
    payload = {
        "sub": subscriber_id,
        "principal_id": subscriber_id,
        "principal_type": "subscriber",
        "session_id": session_id,
        "typ": "access",
        "imp": True,
        "imp_by": acting_subscriber_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl_minutes)).timestamp()),
    }
    return _jwt_encode_token(payload, _jwt_secret(db), _jwt_algorithm(db))


def issue_web_session_token(db: Session | None, access_token: str) -> str:
    """Issue a compact web session JWT for cookie transport.

    Web routes only need principal/session identity. Roles and scopes can be
    resolved server-side when required, which keeps the session cookie small.
    """
    payload = decode_access_token(db, access_token)
    principal_id = str(payload.get("principal_id") or payload.get("sub") or "")
    principal_type = str(payload.get("principal_type") or "subscriber")
    session_id = str(payload.get("session_id") or "")
    if not principal_id or not session_id:
        raise HTTPException(status_code=401, detail="Invalid access token")
    return _issue_access_token(db, principal_id, principal_type, session_id)


def _issue_mfa_token(
    db: Session | None,
    principal_id: str,
    principal_type: str = "subscriber",
    *,
    staff_binding: staff_party_authentication.StaffSessionBinding | None = None,
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
    if principal_type == "system_user":
        if staff_binding is None:
            raise staff_party_authentication.StaffProjectionError(
                staff_party_authentication.StaffProjectionRefusal.projection_missing,
                principal_id,
            )
        if str(staff_binding.system_user_id) != str(principal_id):
            raise staff_party_authentication.StaffProjectionError(
                staff_party_authentication.StaffProjectionRefusal.projection_conflict,
                principal_id,
            )
        payload["party_id"] = str(staff_binding.party_id)
    return _jwt_encode_token(payload, _jwt_secret(db), _jwt_algorithm(db))


def staff_binding_from_token_payload(
    payload: Mapping[str, object],
    *,
    invalid_detail: str,
) -> staff_party_authentication.StaffSessionBinding:
    """Parse the signed staff identity/context pair carried across an MFA hop."""

    principal_id = payload.get("principal_id") or payload.get("sub")
    party_id = payload.get("party_id")
    if not principal_id or not party_id:
        raise HTTPException(status_code=401, detail=invalid_detail)
    try:
        return staff_party_authentication.StaffSessionBinding(
            party_id=UUID(str(party_id)),
            system_user_id=UUID(str(principal_id)),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail=invalid_detail) from exc


# Admin reset links are capped to one hour regardless of the (customer-facing)
# password_reset_expiry_minutes setting; an explicit ttl_minutes still wins.
SYSTEM_USER_RESET_TTL_CAP_MINUTES = 60


def _password_reset_ttl_minutes(db: Session | None) -> int:
    env_value = _env_int("PASSWORD_RESET_EXPIRY_MINUTES")
    if env_value is None:
        env_value = _env_int("PASSWORD_RESET_TTL_MINUTES")
    if env_value is not None:
        return env_value
    value = _setting_value(db, "password_reset_expiry_minutes")
    if value is None:
        value = _setting_value(db, "password_reset_ttl_minutes")
    if value is not None:
        try:
            return int(value)
        except ValueError:
            return 1440
    return 1440


def _issue_password_reset_token(
    db: Session | None,
    principal_id: str,
    principal_type_or_email: str,
    email: str | None = None,
    *,
    ttl_minutes: int | None = None,
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
    token_ttl_minutes = ttl_minutes if ttl_minutes and ttl_minutes > 0 else None
    if token_ttl_minutes is None:
        token_ttl_minutes = _password_reset_ttl_minutes(db)
    payload = {
        "sub": principal_id,
        "principal_id": principal_id,
        "principal_type": principal_type,
        "email": resolved_email,
        "typ": "password_reset",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=token_ttl_minutes)).timestamp()),
    }
    return _jwt_encode_token(payload, _jwt_secret(db), _jwt_algorithm(db))


def _decode_password_reset_token(db: Session | None, token: str) -> dict:
    return _decode_jwt(db, token, "password_reset")


def _email_verification_ttl_minutes(db: Session | None) -> int:
    env_value = _env_int("EMAIL_VERIFICATION_EXPIRY_MINUTES")
    if env_value is None:
        env_value = _env_int("EMAIL_VERIFICATION_TTL_MINUTES")
    if env_value is not None:
        return env_value
    value = _setting_value(db, "email_verification_expiry_minutes")
    if value is None:
        value = _setting_value(db, "email_verification_ttl_minutes")
    if value is not None:
        try:
            return int(value)
        except ValueError:
            return 1440
    return 1440


def _issue_email_verification_token(
    db: Session | None,
    subscriber_id: str,
    email: str,
    *,
    ttl_minutes: int | None = None,
) -> str:
    now = _now()
    token_ttl_minutes = ttl_minutes if ttl_minutes and ttl_minutes > 0 else None
    if token_ttl_minutes is None:
        token_ttl_minutes = _email_verification_ttl_minutes(db)
    payload = {
        "sub": subscriber_id,
        "principal_id": subscriber_id,
        "principal_type": "subscriber",
        "email": email,
        "typ": "email_verification",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=token_ttl_minutes)).timestamp()),
    }
    return _jwt_encode_token(payload, _jwt_secret(db), _jwt_algorithm(db))


def _decode_email_verification_token(db: Session | None, token: str) -> dict:
    return _decode_jwt(db, token, "email_verification")


class InvalidEmailVerificationCapability(ValueError):
    """Safe boundary for the legacy JWT decoder's transport exception."""


class EmailVerificationCapabilityUnavailable(RuntimeError):
    """Safe operational refusal from the legacy decoder boundary."""


@dataclass(frozen=True, slots=True)
class EmailVerificationCapability:
    principal_id: str
    principal_type: str
    subject: str
    purpose: str
    expires_at: int
    email: str = field(repr=False)


def decode_email_verification_capability(
    db: Session, *, token: str
) -> EmailVerificationCapability:
    try:
        payload = _decode_email_verification_token(db, token)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise EmailVerificationCapabilityUnavailable(
                "Email verification capability validation is unavailable"
            ) from None
        raise InvalidEmailVerificationCapability(
            "Invalid verification capability"
        ) from None
    except (TypeError, ValueError, AttributeError):
        # python-jose can raise these for malformed registered claim shapes.
        # Storage/configuration exceptions retain their operational category.
        raise InvalidEmailVerificationCapability(
            "Invalid verification capability"
        ) from None
    keys = ("principal_id", "principal_type", "sub", "typ", "email")
    if (
        any(not isinstance(payload.get(key), str) for key in keys)
        or type(payload.get("exp")) is not int
    ):
        raise InvalidEmailVerificationCapability("Invalid verification capability")
    return EmailVerificationCapability(
        principal_id=payload["principal_id"],
        principal_type=payload["principal_type"],
        subject=payload["sub"],
        purpose=payload["typ"],
        expires_at=payload["exp"],
        email=payload["email"],
    )


def _decode_jwt(db: Session | None, token: str, expected_type: str) -> dict:
    try:
        payload = _jwt_decode_token(token, _jwt_secret(db), _jwt_algorithm(db))
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
    cached = auth_cache.get_claims(principal_type, str(resolved_principal_id))
    if cached is not None:
        return cached
    if principal_type == "reseller_user":
        # Reseller portal authorization is enforced via reseller_users
        # membership (reseller_portal._get_reseller_user), not RBAC roles. A
        # reseller_user principal carries no system/subscriber roles.
        auth_cache.set_claims(principal_type, str(resolved_principal_id), [], [])
        return [], []
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
            .join(
                SystemUserPermission,
                SystemUserPermission.permission_id == Permission.id,
            )
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
    auth_cache.set_claims(
        principal_type,
        str(resolved_principal_id),
        role_names,
        permission_keys,
    )
    return role_names, permission_keys


def _resolve_login_credential(
    db: Session,
    *,
    provider: AuthProvider,
    identifier: str,
) -> UserCredential | None:
    """Resolve an active login credential, including the safe customer alias."""

    credential, _customer_email_alias = _resolve_login_credential_match(
        db,
        provider=provider,
        identifier=identifier,
    )
    return credential


def _resolve_login_credential_match(
    db: Session,
    *,
    provider: AuthProvider,
    identifier: str,
) -> tuple[UserCredential | None, bool]:
    """Resolve a credential and identify contact-email alias use."""

    normalized_identifier = identifier.strip()
    if not normalized_identifier:
        return None, False

    direct_credential = cast(
        UserCredential | None,
        db.query(UserCredential)
        .outerjoin(SystemUser, SystemUser.id == UserCredential.system_user_id)
        .filter(UserCredential.provider == provider)
        .filter(UserCredential.is_active.is_(True))
        .filter(
            (UserCredential.username == normalized_identifier)
            | (func.lower(SystemUser.email) == normalized_identifier.lower())
        )
        .order_by(UserCredential.created_at.desc())
        .first(),
    )
    if direct_credential is not None:
        is_customer_email_login = (
            provider is AuthProvider.local
            and direct_credential.subscriber_id is not None
            and "@" in normalized_identifier
        )
        return direct_credential, is_customer_email_login

    if provider is not AuthProvider.local:
        return None, False

    resolution = customer_login_identity.resolve_customer_login_identity(
        db,
        customer_login_identity.ResolveCustomerLoginIdentity(
            identifier=normalized_identifier
        ),
    )
    refusal = customer_login_identity.resolution_error(resolution)
    if refusal is not None:
        raise refusal
    if (
        resolution.status
        is not customer_login_identity.CustomerLoginResolutionStatus.matched
        or resolution.credential_id is None
    ):
        return None, False

    credential = db.get(UserCredential, resolution.credential_id)
    is_email_alias = resolution.source in {
        customer_login_identity.CustomerLoginMatchSource.case_insensitive_email_username,
        customer_login_identity.CustomerLoginMatchSource.unique_customer_email,
    }
    return credential, is_email_alias


def _principal_for_credential(
    db: Session, credential: UserCredential
) -> tuple[str, str, object | None]:
    if credential.system_user_id:
        # `system_user_id` still discriminates the KIND of principal — staff
        # rather than subscriber or reseller — because SystemUser remains the
        # product-owned staff context. It no longer supplies the IDENTITY: that
        # comes from the Party projection, with no legacy fallback.
        # `staff_party_authentication` raises rather than returning None, so an
        # unresolvable projection cannot be mistaken for an anonymous principal.
        principal = staff_party_authentication.resolve_staff_principal(db, credential)
        return "system_user", str(principal.id), principal
    if (
        getattr(credential, "reseller_user_id", None)
        and settings.reseller_user_principal_enabled
    ):
        return (
            "reseller_user",
            str(credential.reseller_user_id),
            db.get(ResellerUser, credential.reseller_user_id),
        )
    if credential.subscriber_id:
        return (
            "subscriber",
            str(credential.subscriber_id),
            db.get(Subscriber, credential.subscriber_id),
        )
    return "subscriber", "", None


class AccessCredentialVerificationStatus(str, Enum):
    MATCHED = "matched"
    DENIED = "denied"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class AccessCredentialVerificationQuery:
    identifier: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AccessCredentialVerificationOutcome:
    status: AccessCredentialVerificationStatus
    resolved_credential_id: UUID | None = None
    snapshot: password_authentication.PortalAccessSnapshot | None = field(
        default=None, repr=False
    )
    subscriber: Subscriber | None = field(default=None, repr=False)


def _resolve_access_credential_login(
    db: Session, *, identifier: str, password: str
) -> tuple[str, str, Subscriber] | None:
    outcome = _resolve_access_credential_match(
        db,
        query=AccessCredentialVerificationQuery(
            identifier=identifier.strip(), password=password
        ),
    )
    if (
        outcome.status is not AccessCredentialVerificationStatus.MATCHED
        or outcome.subscriber is None
    ):
        return None
    return "subscriber", str(outcome.subscriber.id), outcome.subscriber


def _resolve_access_credential_match(
    db: Session,
    *,
    query: AccessCredentialVerificationQuery,
) -> AccessCredentialVerificationOutcome:
    if not query.identifier:
        return AccessCredentialVerificationOutcome(
            AccessCredentialVerificationStatus.UNKNOWN
        )
    candidates = db.scalars(
        sa_select(AccessCredential)
        .where(AccessCredential.username == query.identifier)
        .where(AccessCredential.is_active.is_(True))
        .order_by(AccessCredential.created_at.desc())
        .limit(2)
    ).all()
    if not candidates:
        return AccessCredentialVerificationOutcome(
            AccessCredentialVerificationStatus.UNKNOWN
        )
    if len(candidates) != 1:
        return AccessCredentialVerificationOutcome(
            AccessCredentialVerificationStatus.AMBIGUOUS
        )
    credential = candidates[0]
    denied = AccessCredentialVerificationOutcome(
        AccessCredentialVerificationStatus.DENIED,
        resolved_credential_id=credential.id,
    )
    if not credential.secret_hash:
        return denied
    try:
        # Preserve the existing PPPoE bcrypt refusal; user verification does
        # not expand the set of admitted network/service secret formats.
        if _is_bcrypt_hash(credential.secret_hash):
            raise ValueError("bcrypt-format PPPoE secret not accepted")
        password_matches = verify_service_secret(query.password, credential.secret_hash)
    except ValueError:
        logger.info("access_credential_verification_unavailable")
        return denied
    if not password_matches:
        return denied
    subscriber = db.get(Subscriber, credential.subscriber_id)
    if subscriber is None:
        return denied
    return AccessCredentialVerificationOutcome(
        AccessCredentialVerificationStatus.MATCHED,
        resolved_credential_id=credential.id,
        snapshot=password_authentication.PortalAccessSnapshot(
            credential.id,
            credential.updated_at,
            credential.secret_hash,
            credential.subscriber_id,
        ),
        subscriber=subscriber,
    )


def _primary_totp_method(
    db: Session, principal_type: str, principal_id: str
) -> MFAMethod | None:
    query = db.query(MFAMethod).filter(MFAMethod.method_type == MFAMethodType.totp)
    if principal_type == "system_user":
        query = query.filter(MFAMethod.system_user_id == coerce_uuid(principal_id))
    elif principal_type == "reseller_user":
        query = query.filter(MFAMethod.reseller_user_id == coerce_uuid(principal_id))
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


def hash_service_secret(password: str) -> str:
    """Store subscriber service credentials in reversible-at-rest format.

    PPPoE and other RADIUS flows may require Cleartext-Password or NT-Password
    for MS-CHAP-compatible auth. We therefore store service credentials using
    the shared credential encryption layer instead of a one-way hash.
    """
    return cast(str, encrypt_credential(password))


def _is_bcrypt_hash(password_hash: str) -> bool:
    return password_hash.startswith(
        _BCRYPT_VERIFY_PREFIXES + _BCRYPT_UNSUPPORTED_PREFIXES
    )


def _verify_legacy_bcrypt(password: str, password_hash: str) -> bool:
    """Verify a legacy bcrypt hash directly with `bcrypt.checkpw`. Never raises.

    Behaviour:
    - $2a$/$2b$/$2y$ hash: bcrypt result (True/False).
    - $2x$ hash: False (unsupported variant).
    - Malformed/truncated hash or bad salt/cost: False.
    - Password longer than 72 UTF-8 *bytes*: False (fail closed; the user must
      reset their password). bcrypt 5 refuses such input and we do not
      truncate or prehash. A structured warning is logged without the
      password or hash.
    """
    if password_hash.startswith(_BCRYPT_UNSUPPORTED_PREFIXES):
        CREDENTIAL_FORMAT_REJECTIONS.inc()
        return False
    password_bytes = password.encode("utf-8", errors="surrogatepass")
    if len(password_bytes) > _BCRYPT_MAX_PASSWORD_BYTES:
        logger.warning(
            "bcrypt verification refused: password exceeds 72 bytes",
            extra={
                "event": "auth.bcrypt_password_too_long",
                "hash_scheme": password_hash[:4],
            },
        )
        return False
    try:
        return bool(bcrypt.checkpw(password_bytes, password_hash.encode("utf-8")))
    except (ValueError, TypeError):
        CREDENTIAL_FORMAT_REJECTIONS.inc()
        return False


def _verify_legacy_stored_credential(password: str, stored: str | None) -> bool:
    """Verification-only compatibility; no representation or protocol writes."""
    if not stored:
        return False
    try:
        decrypted = decrypt_credential(stored)
        if decrypted != stored:
            return secrets.compare_digest(password, decrypted or "")
        if _is_bcrypt_hash(stored):
            return _verify_legacy_bcrypt(password, stored)
        return cast(bool, PASSWORD_CONTEXT.verify(password, stored))
    except (ValueError, TypeError, UnicodeError):
        # Includes unknown/malformed hashes and unavailable reversible values.
        # Never attach the submitted identifier, secret or hash to telemetry.
        CREDENTIAL_FORMAT_REJECTIONS.inc()
        return False


def verify_user_password_outcome(
    request: user_passwords.UserPasswordVerification,
) -> user_passwords.PasswordVerificationOutcome:
    """Typed user-only policy boundary; no upgrade, notice, or persistence."""
    return user_passwords.verify_user_password(
        request, legacy_verify=_verify_legacy_stored_credential
    )


def verify_user_password(
    password: str,
    password_hash: str | None,
    *,
    reversible_policy: user_passwords.ReversiblePasswordPolicy = (
        user_passwords.ReversiblePasswordPolicy.COMPAT
    ),
    argon2_limits: user_passwords.Argon2idVerificationLimits | None = None,
) -> bool:
    """Compatibility adapter; refusal is explicit, never a runtime policy flip."""
    return verify_user_password_outcome(
        user_passwords.UserPasswordVerification(
            password=password,
            stored=password_hash,
            reversible_policy=reversible_policy,
            argon2_limits=argon2_limits,
        )
    ).accepted


def verify_service_secret(password: str, secret_hash: str | None) -> bool:
    """Verify AccessCredential material without changing storage or protocols.

    The existing shared PPPoE fallback refuses bcrypt; keep that acceptance
    boundary even though user-password verification supports legacy bcrypt.
    The portal's separate decrypt-and-compare fallback remains unchanged.
    """
    if secret_hash and _is_bcrypt_hash(secret_hash):
        return False
    return _verify_legacy_stored_credential(password, secret_hash)


LOGIN_MAX_FAILED_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15


def _admin_login_max_failed_attempts(db: Session | None) -> int:
    return _setting_int(
        db,
        "admin_login_max_attempts",
        LOGIN_MAX_FAILED_ATTEMPTS,
    )


def _admin_login_lockout_minutes(db: Session | None) -> int:
    return _setting_int(
        db,
        "admin_lockout_minutes",
        LOGIN_LOCKOUT_MINUTES,
    )


def _login_failure_policy(db: Session | None) -> password_authentication.FailurePolicy:
    return password_authentication.FailurePolicy(
        max_attempts=_admin_login_max_failed_attempts(db),
        lock_minutes=_admin_login_lockout_minutes(db),
    )


MFA_MAX_FAILED_ATTEMPTS = 5
MFA_LOCKOUT_MINUTES = 15
MFA_RECOVERY_CODE_COUNT = 10
MFA_RECOVERY_CODE_ALPHABET = "23456789" + string.ascii_uppercase.replace(
    "O", ""
).replace("I", "")


def _mfa_max_failed_attempts(db: Session | None) -> int:
    return _setting_int(
        db,
        "mfa_max_failed_attempts",
        MFA_MAX_FAILED_ATTEMPTS,
    )


def _mfa_lockout_minutes(db: Session | None) -> int:
    return _setting_int(
        db,
        "mfa_lockout_minutes",
        MFA_LOCKOUT_MINUTES,
    )


def password_min_length(db: Session | None = None) -> int:
    return _setting_int(db, "password_min_length", 8)


# Privileged (staff/admin) accounts use the same configurable minimum as other
# local accounts. The shared default is eight characters.
SYSTEM_USER_PASSWORD_MIN_LENGTH = 8


def password_min_length_for(db: Session | None, principal_type: str | None) -> int:
    """Minimum password length for a principal type.

    ``system_user`` (staff/admin) principals get ``max(global minimum,
    system_user floor)``; everyone else gets the global minimum.
    """
    base = password_min_length(db)
    if principal_type == "system_user":
        floor = _setting_int(
            db, "system_user_password_min_length", SYSTEM_USER_PASSWORD_MIN_LENGTH
        )
        return max(base, floor)
    return base


def password_policy_violations(password: str, minimum: int) -> tuple[str, ...]:
    """Return the unmet local-password requirements in display order."""

    violations: list[str] = []
    if len(password) < minimum:
        violations.append(f"Password must be at least {minimum} characters.")
    if not any(character.isupper() for character in password):
        violations.append("Password must include at least one uppercase letter.")
    if not any(character.islower() for character in password):
        violations.append("Password must include at least one lowercase letter.")
    if not any(character.isdigit() for character in password):
        violations.append("Password must include at least one number.")
    if not any(
        character.isascii() and not character.isalnum() for character in password
    ):
        violations.append("Password must include at least one special character.")
    return tuple(violations)


def _refuse_revoked_mfa_method(method: MFAMethod) -> None:
    """Confirm flows activate only pending setup rows, never revoked factors.

    An administratively disabled method (inactive, secret cleared) must not be
    revived by whoever still holds its old seed.
    """

    if not method.is_active or not method.secret:
        raise HTTPException(status_code=403, detail="MFA method not allowed")


def ensure_mfa_not_locked(method: MFAMethod) -> None:
    locked_until = _as_utc(method.locked_until)
    if locked_until and locked_until > _now():
        raise HTTPException(
            status_code=429,
            detail=lockout_detail(
                "Too many incorrect codes", locked_until=locked_until
            ),
        )


def _mfa_failure_policy(db: Session | None) -> password_authentication.FailurePolicy:
    return password_authentication.FailurePolicy(
        max_attempts=_mfa_max_failed_attempts(db),
        lock_minutes=_mfa_lockout_minutes(db),
    )


def record_mfa_failure(
    db: Session, method: MFAMethod, *, request: Request | None = None
) -> None:
    """Enrollment failure uses the independent counter-and-audit owner."""
    password_authentication.record_mfa_failure(
        db,
        method.id,
        _mfa_failure_policy(db),
        request=request,
        surface=password_authentication.DenialSurface.ENROLLMENT,
    )


def _record_denial(
    *,
    reason: password_authentication.DenialReason,
    target: password_authentication.DenialTarget | None,
    request: Request | None = None,
    phase: password_authentication.DenialPhase = password_authentication.DenialPhase.PASSWORD,
    surface: password_authentication.DenialSurface = password_authentication.DenialSurface.LOGIN,
    status_code: Literal[400, 401, 403, 404, 409, 428, 429, 503] = 401,
    effect: password_authentication.PasswordFailureEffect
    | password_authentication.MfaFailureEffect
    | None = None,
) -> None:
    password_authentication.record_authentication_denial(
        password_authentication.denial_for_request(
            reason=reason,
            surface=surface,
            phase=phase,
            target=target,
            request=request,
            status_code=status_code,
            effect=effect,
        )
    )


def _owner_denial_status(
    exc: password_authentication.PasswordAuthenticationError,
) -> Literal[401, 403, 409, 428, 429, 503]:
    if exc.kind in {
        "locked",
        "account_disabled",
        "admin_required",
        "customer_required",
    }:
        return 403
    if exc.kind == "must_change_password":
        return 428
    if exc.kind == "mfa_locked":
        return 429
    if exc.kind == "credential_changed":
        return 409
    if exc.kind == "lock_timeout":
        return 503
    return 401


def _owner_denial_reason(
    exc: password_authentication.PasswordAuthenticationError,
) -> password_authentication.DenialReason:
    try:
        return password_authentication.DenialReason(exc.kind)
    except ValueError:
        return password_authentication.DenialReason.OWNER_UNAVAILABLE


def record_mfa_success(method: MFAMethod) -> None:
    method.failed_attempts = 0
    method.locked_until = None


def _normalize_recovery_code(code: str) -> str:
    return "".join(ch for ch in code.strip().upper() if ch.isalnum())


def _recovery_code_hash(code: str) -> str:
    normalized = _normalize_recovery_code(code)
    return _hash_token(f"mfa-recovery:{normalized}")


def _new_recovery_code() -> str:
    raw = "".join(secrets.choice(MFA_RECOVERY_CODE_ALPHABET) for _ in range(10))
    return f"{raw[:5]}-{raw[5:]}"


def stage_mfa_recovery_codes(
    db: Session,
    method_id: UUID | str,
    count: int = MFA_RECOVERY_CODE_COUNT,
) -> list[str]:
    """Replace a method's recovery codes WITHOUT committing; plaintext returned.

    The caller owns the transaction: forced enrollment stages these inside the
    credential-gated owner transaction so a refusal discards them.
    """

    db.query(MFARecoveryCode).filter(
        MFARecoveryCode.mfa_method_id == coerce_uuid(method_id),
        MFARecoveryCode.is_active.is_(True),
        MFARecoveryCode.used_at.is_(None),
    ).update({"is_active": False})

    codes: list[str] = []
    seen_hashes: set[str] = set()
    for _ in range(count):
        code = _new_recovery_code()
        code_hash = _recovery_code_hash(code)
        while code_hash in seen_hashes:
            code = _new_recovery_code()
            code_hash = _recovery_code_hash(code)
        seen_hashes.add(code_hash)
        codes.append(code)
        db.add(
            MFARecoveryCode(
                mfa_method_id=coerce_uuid(method_id),
                code_hash=code_hash,
                is_active=True,
            )
        )
    return codes


def generate_mfa_recovery_codes(
    db: Session,
    method: MFAMethod | str,
    count: int = MFA_RECOVERY_CODE_COUNT,
) -> list[str]:
    """Replace recovery codes for an MFA method and return plaintext once."""
    mfa_method = (
        method
        if isinstance(method, MFAMethod)
        else db.get(MFAMethod, coerce_uuid(method))
    )
    if not mfa_method:
        raise HTTPException(status_code=404, detail="MFA method not found")

    codes = stage_mfa_recovery_codes(db, mfa_method.id, count)
    db.commit()
    return codes


def _candidate_recovery_code_hash(code: str) -> str | None:
    """Hash of a well-formed recovery-code candidate, else ``None``.

    The code is SPENT only inside the MFA completion transaction by a
    conditional UPDATE (``password_authentication.complete_mfa``), so two
    concurrent submissions cannot both consume one code.
    """

    normalized = _normalize_recovery_code(code)
    if len(normalized) < 8:
        return None
    return _recovery_code_hash(normalized)


def _http_from_refusal(
    exc: password_authentication.PasswordAuthenticationError,
) -> HTTPException:
    """Adapter mapping for the credential-standing owner's refusals."""

    kind = exc.kind
    if kind == "locked":
        return HTTPException(
            status_code=403,
            detail=lockout_detail(
                "Account locked", locked_until=exc.details.get("locked_until")
            ),
        )
    if kind == "must_change_password":
        return HTTPException(
            status_code=428,
            detail={
                "code": "PASSWORD_RESET_REQUIRED",
                "message": "Password reset required",
            },
        )
    if kind == "account_disabled":
        return HTTPException(status_code=403, detail="Account disabled")
    if kind == "customer_required":
        return HTTPException(
            status_code=403,
            detail={"code": exc.code, "message": exc.message},
        )
    if kind == "admin_required":
        return HTTPException(
            status_code=403,
            detail="Administrator access is required for this area.",
        )
    if kind == "invalid_mfa_token":
        return HTTPException(status_code=401, detail="Invalid MFA token")
    if kind == "invalid_mfa_code":
        return HTTPException(status_code=401, detail="Invalid MFA code")
    if kind == "mfa_locked":
        return HTTPException(
            status_code=429,
            detail=lockout_detail(
                "Too many incorrect codes",
                locked_until=exc.details.get("locked_until"),
            ),
        )
    if kind == "credential_changed":
        return HTTPException(
            status_code=409, detail="Credential changed; sign in again"
        )
    if kind == "lock_timeout":
        return HTTPException(
            status_code=503,
            detail="Service busy; retry shortly",
            headers={
                "Retry-After": str(
                    password_authentication.LOCK_TIMEOUT_RETRY_AFTER_SECONDS
                )
            },
        )
    return HTTPException(status_code=401, detail="Invalid credentials")


class AuthFlow(ListResponseMixin):
    @staticmethod
    def _response_with_refresh_cookie(
        db: Session | None,
        payload: dict | TokenResponse,
        model_cls,
        status_code: int = status.HTTP_200_OK,
    ) -> Response:
        settings = AuthFlow.refresh_cookie_settings(db)
        payload_values = (
            payload.model_dump() if isinstance(payload, TokenResponse) else payload
        )
        body_payload = {**payload_values, "refresh_token": None}  # nosec
        body_content = model_cls(**body_payload).model_dump_json()
        response = Response(
            content=body_content,
            status_code=status_code,
            media_type="application/json",
        )
        refresh_token = payload_values.get("refresh_token")
        if isinstance(refresh_token, str) and refresh_token:
            response.set_cookie(
                key=settings["key"],
                value=refresh_token,
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
        db: Session,
        username: str,
        password: str,
        request: Request,
        provider: str | None,
    ):
        result = AuthFlow.login(db, username, password, request, provider)
        if result.get("refresh_token") and not wants_refresh_in_body(request):
            return AuthFlow._response_with_refresh_cookie(
                db, result, LoginResponse, status.HTTP_200_OK
            )
        # Mobile clients (header set) receive the refresh token in the body.
        return result

    @staticmethod
    def login(
        db: Session,
        username: str,
        password: str,
        request: Request,
        provider: str | None,
        *,
        audience: LoginAudience = LoginAudience.general,
        denial_surface: password_authentication.DenialSurface = password_authentication.DenialSurface.LOGIN,
    ):
        if isinstance(provider, AuthProvider):
            provider_value = provider.value
        else:
            provider_value = provider or AuthProvider.local.value
        try:
            resolved_provider = AuthProvider(provider_value)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid auth provider"
            ) from exc
        if resolved_provider not in (AuthProvider.radius, AuthProvider.local):
            raise HTTPException(status_code=400, detail="Unsupported auth provider")
        try:
            credential, customer_email_alias = _resolve_login_credential_match(
                db,
                provider=resolved_provider,
                identifier=username,
            )
        except customer_login_identity.CustomerLoginIdentityError as exc:
            _record_denial(
                reason=password_authentication.DenialReason.INVALID_CREDENTIALS,
                target=None,
                request=request,
                surface=denial_surface,
            )
            if exc.code == customer_login_identity.AMBIGUOUS_EMAIL_CODE:
                raise HTTPException(status_code=409, detail=exc.message) from exc
            raise HTTPException(status_code=401, detail="Invalid credentials") from exc

        # Phase A (read-only). Check the lock before verifying the password: a
        # locked account must answer identically to right and wrong passwords
        # (no correctness oracle), and attempts made while locked must not
        # extend the lock. An EXPIRED lock is not reset here: the dirty ORM
        # write this used to make was committed by whatever ran next. The
        # atomic statements in `password_authentication` handle expiry.
        now = _now()
        authenticated_access_credential = False
        access_credential: password_authentication.PortalAccessSnapshot | None = None
        principal_type: str
        principal_id: str
        principal: object | None
        failure_policy = _login_failure_policy(db)
        # Snapshot the standing this verification is about, from the SAME row
        # read as the hash we verify. Nothing between here and the commit gate
        # may re-read it: a refreshed ORM attribute would silently adopt a
        # newer version and turn the gate into a no-op.
        credential_id = credential.id if credential else None
        snapshot_version = int(credential.credential_version) if credential else None
        snapshot_hash = credential.password_hash if credential else None
        snapshot_provider = credential.provider if credential else None

        denial_target = (
            password_authentication.DenialTarget(
                password_authentication.DenialTargetKind.CREDENTIAL, credential_id
            )
            if credential_id is not None
            else None
        )
        if credential:
            locked_until = _as_utc(credential.locked_until)
            if locked_until and locked_until > now:
                _record_denial(
                    reason=password_authentication.DenialReason.LOCKED,
                    target=denial_target,
                    request=request,
                    surface=denial_surface,
                    status_code=403,
                )
                raise HTTPException(
                    status_code=403,
                    detail=lockout_detail("Account locked", locked_until=locked_until),
                )

        if credential and resolved_provider == AuthProvider.radius:
            try:
                radius_auth_service.authenticate(
                    db,
                    str(credential.username or username),
                    password,
                    str(credential.radius_server_id)
                    if credential.radius_server_id
                    else None,
                )
            except HTTPException as exc:
                if exc.status_code in (401, 403):
                    password_authentication.record_password_failure(
                        db,
                        credential_id,
                        failure_policy,
                        request=request,
                        surface=denial_surface,
                    )
                raise
        elif credential and verify_user_password(password, snapshot_hash):
            pass
        else:
            access_match = AccessCredentialVerificationOutcome(
                AccessCredentialVerificationStatus.UNKNOWN
            )
            if (
                resolved_provider == AuthProvider.local
                and not customer_email_alias
                and (credential is None or credential.subscriber_id is not None)
            ):
                access_match = _resolve_access_credential_match(
                    db,
                    query=AccessCredentialVerificationQuery(
                        identifier=username.strip(), password=password
                    ),
                )
            if access_match.status is not AccessCredentialVerificationStatus.MATCHED:
                if credential:
                    password_authentication.record_password_failure(
                        db,
                        credential_id,
                        failure_policy,
                        request=request,
                        surface=denial_surface,
                    )
                else:
                    _record_denial(
                        reason=password_authentication.DenialReason.INVALID_CREDENTIALS,
                        target=password_authentication.DenialTarget(
                            password_authentication.DenialTargetKind.ACCESS,
                            access_match.resolved_credential_id,
                        )
                        if access_match.resolved_credential_id is not None
                        else None,
                        request=request,
                        surface=denial_surface,
                    )
                raise HTTPException(status_code=401, detail="Invalid credentials")
            assert (
                access_match.snapshot is not None
                and access_match.subscriber is not None
            )
            access_credential = access_match.snapshot
            principal_type, principal_id, principal = (
                "subscriber",
                str(access_match.subscriber.id),
                access_match.subscriber,
            )
            access_secret_hash = access_credential.secret_hash
            access_updated_at = access_credential.updated_at
            authenticated_access_credential = True
            denial_target = password_authentication.DenialTarget(
                password_authentication.DenialTargetKind.ACCESS, access_credential.id
            )

        # Eligibility is decided BEFORE any successful-login mutation, and
        # AFTER password verification deliberately: checking it first would
        # answer an unauthenticated caller differently for a disabled account
        # than for a wrong password (an account-state oracle).
        if not authenticated_access_credential:
            assert credential is not None
            try:
                principal_type, principal_id, principal = _principal_for_credential(
                    db, credential
                )
            except staff_party_authentication.StaffProjectionError as exc:
                # Fail closed. A staff credential whose Party projection is missing,
                # conflicting or ambiguous does not authenticate — it does not fall
                # back to the legacy principal key. The refusal code is logged for
                # the operator; the caller is told only "Account disabled", so this
                # cannot be used to probe projection state.
                _record_denial(
                    reason=password_authentication.DenialReason.PROJECTION,
                    target=denial_target,
                    request=request,
                    surface=denial_surface,
                    status_code=403,
                )
                raise HTTPException(status_code=403, detail="Account disabled") from exc
        refusal = principal_refusal(principal_type, principal, audience)
        if refusal == "account_disabled":
            _record_denial(
                reason=password_authentication.DenialReason.DISABLED,
                target=denial_target,
                request=request,
                surface=denial_surface,
                status_code=403,
            )
            raise HTTPException(status_code=403, detail="Account disabled")
        if refusal == "admin_required":
            _record_denial(
                reason=password_authentication.DenialReason.AUDIENCE,
                target=denial_target,
                request=request,
                surface=denial_surface,
                status_code=403,
            )
            # Verify credentials before this refusal to avoid turning the admin
            # login into an account-type oracle. The rejection still happens
            # before any successful-login mutation or session issuance.
            raise HTTPException(
                status_code=403,
                detail="Administrator access is required for this area.",
            )
        staff_binding = (
            staff_party_authentication.binding_for_principal(principal)
            if principal_type == "system_user"
            else None
        )

        if (
            credential
            and not authenticated_access_credential
            and credential.must_change_password
        ):
            _record_denial(
                reason=password_authentication.DenialReason.RESET_REQUIRED,
                target=denial_target,
                request=request,
                surface=denial_surface,
                status_code=428,
            )
            raise HTTPException(
                status_code=428,
                detail={
                    "code": "PASSWORD_RESET_REQUIRED",
                    "message": "Password reset required",
                },
            )

        mfa_required = (
            _primary_totp_method(db, principal_type, principal_id) is not None
        )
        enrollment_required = (
            not mfa_required
            and principal_type == "system_user"
            and _force_admin_mfa(db)
        )
        if authenticated_access_credential:
            # R8: a PPPoE-secret login never reads or writes the local
            # UserCredential counters, last_login_at or version.
            assert access_credential is not None
            verified = password_authentication.VerifiedCredential(
                src=password_authentication.SRC_ACCESS_CREDENTIAL,
                principal_type=principal_type,
                principal_id=principal_id,
                audience=audience.value,
                staff_binding=staff_binding,
                mfa_required=mfa_required,
                enrollment_required=enrollment_required,
                verified_hash=access_secret_hash,
                access_credential_id=access_credential.id,
                access_credential_updated_at=access_updated_at,
            )
        else:
            assert credential is not None
            verified = password_authentication.VerifiedCredential(
                src=password_authentication.SRC_USER_CREDENTIAL,
                principal_type=principal_type,
                principal_id=principal_id,
                audience=audience.value,
                staff_binding=staff_binding,
                mfa_required=mfa_required,
                enrollment_required=enrollment_required,
                credential_id=credential_id,
                credential_version=snapshot_version,
                provider=snapshot_provider,
                verified_hash=snapshot_hash,
            )

        # Phase B: one owner transaction (credential-standing gate, then the
        # session or challenge, plus audit). Tokens leave only after commit.
        try:
            outcome = password_authentication.complete_password_step(
                db, verified, request=request
            )
        except password_authentication.PasswordAuthenticationError as exc:
            _record_denial(
                reason=_owner_denial_reason(exc),
                status_code=_owner_denial_status(exc),
                target=denial_target,
                request=request,
                surface=denial_surface,
                phase=password_authentication.DenialPhase.OWNER,
            )
            raise _http_from_refusal(exc) from exc
        if outcome.kind == "mfa_challenge":
            return {"mfa_required": True, "mfa_token": outcome.challenge_token}
        if outcome.kind == "enrollment_challenge":
            return {
                "mfa_enrollment_required": True,
                "mfa_enrollment_token": outcome.challenge_token,
            }
        return tokens_for_staged_session(outcome.staged_session)

    @staticmethod
    def admin_mfa_setup(db: Session, system_user_id: str, label: str | None):
        system_user = cast(
            SystemUser | None, db.get(SystemUser, coerce_uuid(system_user_id))
        )
        if not system_user:
            raise HTTPException(status_code=404, detail="System user not found")

        username = system_user.email
        credential = (
            db.query(UserCredential)
            .filter(UserCredential.system_user_id == system_user.id)
            .filter(UserCredential.provider == AuthProvider.local)
            .first()
        )
        if credential and credential.username:
            username = credential.username

        secret = pyotp.random_base32()
        encrypted = _encrypt_secret(db, secret)
        # Reuse a pending (never confirmed) setup row instead of inserting a
        # new one on every visit to the setup page.
        method = (
            db.query(MFAMethod)
            .filter(MFAMethod.system_user_id == system_user.id)
            .filter(MFAMethod.method_type == MFAMethodType.totp)
            .filter(MFAMethod.enabled.is_(False))
            .filter(MFAMethod.verified_at.is_(None))
            .filter(MFAMethod.is_active.is_(True))
            .order_by(MFAMethod.created_at.desc())
            .first()
        )
        if method:
            method.label = label
            method.secret = encrypted
        else:
            method = MFAMethod(
                system_user_id=system_user.id,
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
        otpauth_uri = totp.provisioning_uri(name=username, issuer_name=_totp_issuer(db))
        return {"method_id": method.id, "secret": secret, "otpauth_uri": otpauth_uri}

    @staticmethod
    def admin_mfa_confirm(
        db: Session,
        method_id: str,
        code: str,
        system_user_id: str,
        *,
        request: Request | None = None,
    ):
        method = db.get(MFAMethod, coerce_uuid(method_id))
        if not method:
            _record_denial(
                request=request,
                reason=password_authentication.DenialReason.METHOD,
                target=None,
                phase=password_authentication.DenialPhase.ENROLLMENT,
                surface=password_authentication.DenialSurface.ENROLLMENT,
                status_code=404,
            )
            raise HTTPException(status_code=404, detail="MFA method not found")
        denial_target = password_authentication.DenialTarget(
            password_authentication.DenialTargetKind.METHOD, method.id
        )
        if method.subscriber_id is not None:
            _record_denial(
                request=request,
                reason=password_authentication.DenialReason.METHOD,
                target=denial_target,
                phase=password_authentication.DenialPhase.ENROLLMENT,
                surface=password_authentication.DenialSurface.ENROLLMENT,
                status_code=403,
            )
            raise HTTPException(status_code=403, detail="MFA method not allowed")
        if str(method.system_user_id) != str(system_user_id):
            _record_denial(
                request=request,
                reason=password_authentication.DenialReason.METHOD,
                target=denial_target,
                phase=password_authentication.DenialPhase.ENROLLMENT,
                surface=password_authentication.DenialSurface.ENROLLMENT,
                status_code=403,
            )
            raise HTTPException(status_code=403, detail="MFA method not allowed")
        if method.method_type != MFAMethodType.totp:
            _record_denial(
                request=request,
                reason=password_authentication.DenialReason.METHOD,
                target=denial_target,
                phase=password_authentication.DenialPhase.ENROLLMENT,
                surface=password_authentication.DenialSurface.ENROLLMENT,
                status_code=400,
            )
            raise HTTPException(status_code=400, detail="Unsupported MFA method")

        _refuse_revoked_mfa_method(method)
        try:
            ensure_mfa_not_locked(method)
        except HTTPException:
            _record_denial(
                request=request,
                reason=password_authentication.DenialReason.MFA_LOCKED,
                target=denial_target,
                phase=password_authentication.DenialPhase.ENROLLMENT,
                surface=password_authentication.DenialSurface.ENROLLMENT,
                status_code=429,
            )
            raise
        secret = _decrypt_secret(db, method.secret or "")
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=0):
            record_mfa_failure(db, method, request=request)
            raise HTTPException(status_code=401, detail="Invalid MFA code")
        record_mfa_success(method)

        db.query(MFAMethod).filter(
            MFAMethod.system_user_id == method.system_user_id,
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
    def reseller_mfa_setup(db: Session, reseller_user_id: str, label: str | None):
        """TOTP enrolment for a first-class reseller_user principal (Layer 3)."""
        reseller_user = db.get(ResellerUser, coerce_uuid(reseller_user_id))
        if not reseller_user:
            raise HTTPException(status_code=404, detail="Reseller user not found")

        username = reseller_user.email or str(reseller_user.id)
        credential = (
            db.query(UserCredential)
            .filter(UserCredential.reseller_user_id == reseller_user.id)
            .filter(UserCredential.provider == AuthProvider.local)
            .first()
        )
        if credential and credential.username:
            username = credential.username

        secret = pyotp.random_base32()
        encrypted = _encrypt_secret(db, secret)
        method = (
            db.query(MFAMethod)
            .filter(MFAMethod.reseller_user_id == reseller_user.id)
            .filter(MFAMethod.method_type == MFAMethodType.totp)
            .filter(MFAMethod.enabled.is_(False))
            .filter(MFAMethod.verified_at.is_(None))
            .filter(MFAMethod.is_active.is_(True))
            .order_by(MFAMethod.created_at.desc())
            .first()
        )
        if method:
            method.label = label
            method.secret = encrypted
        else:
            method = MFAMethod(
                reseller_user_id=reseller_user.id,
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
        otpauth_uri = totp.provisioning_uri(name=username, issuer_name=_totp_issuer(db))
        return {"method_id": method.id, "secret": secret, "otpauth_uri": otpauth_uri}

    @staticmethod
    def reseller_mfa_confirm(
        db: Session,
        method_id: str,
        code: str,
        reseller_user_id: str,
        *,
        request: Request | None = None,
    ):
        method = db.get(MFAMethod, coerce_uuid(method_id))
        if not method:
            _record_denial(
                request=request,
                reason=password_authentication.DenialReason.METHOD,
                target=None,
                phase=password_authentication.DenialPhase.ENROLLMENT,
                surface=password_authentication.DenialSurface.ENROLLMENT,
                status_code=404,
            )
            raise HTTPException(status_code=404, detail="MFA method not found")
        denial_target = password_authentication.DenialTarget(
            password_authentication.DenialTargetKind.METHOD, method.id
        )
        if str(method.reseller_user_id) != str(reseller_user_id):
            _record_denial(
                request=request,
                reason=password_authentication.DenialReason.METHOD,
                target=denial_target,
                phase=password_authentication.DenialPhase.ENROLLMENT,
                surface=password_authentication.DenialSurface.ENROLLMENT,
                status_code=403,
            )
            raise HTTPException(status_code=403, detail="MFA method not allowed")
        if method.method_type != MFAMethodType.totp:
            _record_denial(
                request=request,
                reason=password_authentication.DenialReason.METHOD,
                target=denial_target,
                phase=password_authentication.DenialPhase.ENROLLMENT,
                surface=password_authentication.DenialSurface.ENROLLMENT,
                status_code=400,
            )
            raise HTTPException(status_code=400, detail="Unsupported MFA method")

        _refuse_revoked_mfa_method(method)
        try:
            ensure_mfa_not_locked(method)
        except HTTPException:
            _record_denial(
                request=request,
                reason=password_authentication.DenialReason.MFA_LOCKED,
                target=denial_target,
                phase=password_authentication.DenialPhase.ENROLLMENT,
                surface=password_authentication.DenialSurface.ENROLLMENT,
                status_code=429,
            )
            raise
        secret = _decrypt_secret(db, method.secret or "")
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=0):
            record_mfa_failure(db, method, request=request)
            raise HTTPException(status_code=401, detail="Invalid MFA code")
        record_mfa_success(method)

        db.query(MFAMethod).filter(
            MFAMethod.reseller_user_id == method.reseller_user_id,
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
    def mfa_verify(
        db: Session,
        mfa_token: str,
        code: str,
        request: Request,
        *,
        audience: LoginAudience = LoginAudience.general,
        denial_surface: password_authentication.DenialSurface = password_authentication.DenialSurface.LOGIN,
    ):
        # Phase A (read-only): validate the v2 challenge (no legacy fallback:
        # a token without the credential binding is refused), resolve the
        # principal and method, and check the second factor. Nothing is
        # consumed or counted here.
        try:
            challenge = password_authentication.decode_challenge(
                db, mfa_token, expected_typ="mfa"
            )
        except password_authentication.PasswordAuthenticationError as exc:
            _record_denial(
                reason=password_authentication.DenialReason.INVALID_CHALLENGE,
                target=None,
                request=request,
                surface=denial_surface,
                phase=password_authentication.DenialPhase.MFA,
            )
            raise _http_from_refusal(exc) from exc
        denial_target = password_authentication.challenge_denial_target(db, challenge)
        if challenge.audience != audience.value:
            _record_denial(
                reason=password_authentication.DenialReason.AUDIENCE,
                target=denial_target,
                request=request,
                surface=denial_surface,
                phase=password_authentication.DenialPhase.MFA,
            )
            raise HTTPException(status_code=401, detail="Invalid MFA token")
        principal_id = challenge.principal_id
        principal_type = challenge.principal_type
        principal: object | None = None
        staff_binding: staff_party_authentication.StaffSessionBinding | None = None
        if principal_type == "system_user":
            try:
                staff_binding = staff_binding_from_token_payload(
                    {"principal_id": principal_id, "party_id": challenge.party_id},
                    invalid_detail="Invalid MFA token",
                )
            except HTTPException:
                _record_denial(
                    reason=password_authentication.DenialReason.INVALID_CHALLENGE,
                    target=denial_target,
                    request=request,
                    surface=denial_surface,
                    phase=password_authentication.DenialPhase.MFA,
                )
                raise
            try:
                principal = staff_party_authentication.resolve_staff_principal_by_party(
                    db,
                    staff_binding.party_id,
                    staff_binding.system_user_id,
                    reference=staff_binding.system_user_id,
                )
            except staff_party_authentication.StaffProjectionError as exc:
                _record_denial(
                    reason=password_authentication.DenialReason.PROJECTION,
                    target=denial_target,
                    request=request,
                    surface=denial_surface,
                    phase=password_authentication.DenialPhase.MFA,
                )
                raise HTTPException(
                    status_code=401,
                    detail="Invalid MFA token",
                ) from exc
            principal_id = str(principal.id)

        if audience is LoginAudience.admin and not is_admin_portal_principal(
            str(principal_type), principal
        ):
            _record_denial(
                reason=password_authentication.DenialReason.AUDIENCE,
                target=denial_target,
                request=request,
                surface=denial_surface,
                phase=password_authentication.DenialPhase.MFA,
                status_code=403,
            )
            raise HTTPException(
                status_code=403,
                detail="Administrator access is required for this area.",
            )

        method = _primary_totp_method(db, principal_type, str(principal_id))
        if not method:
            _record_denial(
                reason=password_authentication.DenialReason.METHOD,
                target=denial_target,
                request=request,
                surface=denial_surface,
                phase=password_authentication.DenialPhase.MFA,
                status_code=404,
            )
            raise HTTPException(status_code=404, detail="MFA method not found")

        method_id = method.id
        method_target = password_authentication.DenialTarget(
            password_authentication.DenialTargetKind.METHOD, method_id
        )
        try:
            ensure_mfa_not_locked(method)
        except HTTPException:
            _record_denial(
                reason=password_authentication.DenialReason.MFA_LOCKED,
                target=method_target,
                request=request,
                surface=denial_surface,
                phase=password_authentication.DenialPhase.MFA,
                status_code=429,
            )
            raise
        secret = _decrypt_secret(db, method.secret or "")
        totp = pyotp.TOTP(secret)
        # Customer native login keeps the customer portal's TOTP-only policy
        # and whitespace handling. Other audiences retain recovery-code login.
        customer_audience = audience is LoginAudience.customer
        totp_ok = bool(
            totp.verify(code.strip() if customer_audience else code, valid_window=0)
        )
        recovery_hash = (
            None
            if totp_ok or customer_audience
            else _candidate_recovery_code_hash(code)
        )
        failure_policy = _mfa_failure_policy(db)

        # Phase B: one owner transaction (credential gate, eligibility,
        # atomic recovery-code spend, method update, session, audit).
        try:
            staged = password_authentication.complete_mfa(
                db,
                challenge,
                request=request,
                method_id=method_id,
                totp_ok=totp_ok,
                recovery_code_hash=recovery_hash,
                failure_policy=failure_policy,
            )
        except password_authentication.PasswordAuthenticationError as exc:
            effect = (
                password_authentication.MfaFailureEffect(method_id, failure_policy)
                if exc.kind == "invalid_mfa_code"
                else None
            )
            _record_denial(
                reason=_owner_denial_reason(exc),
                status_code=_owner_denial_status(exc),
                target=method_target if effect else denial_target,
                request=request,
                surface=denial_surface,
                phase=password_authentication.DenialPhase.OWNER,
                effect=effect,
            )
            raise _http_from_refusal(exc) from exc
        return tokens_for_staged_session(staged)

    @staticmethod
    def confirm_mfa_enrollment(
        db: Session,
        enrollment_token: str,
        method_id: str,
        code: str,
        request: Request,
    ) -> tuple[dict[str, str], list[str]]:
        """Forced enrollment: verify the code, then ONE credential-gated commit.

        The enrollment token is a v2 challenge bound to the credential id and
        version (no legacy fallback). Phase A only reads; the factor, recovery
        codes, session and audit are all written inside the owner transaction,
        so a reset since the password step leaves nothing behind.
        """

        try:
            challenge = password_authentication.decode_challenge(
                db, enrollment_token, expected_typ="mfa_enrollment"
            )
        except password_authentication.PasswordAuthenticationError as exc:
            _record_denial(
                reason=password_authentication.DenialReason.INVALID_CHALLENGE,
                target=None,
                request=request,
                phase=password_authentication.DenialPhase.ENROLLMENT,
                surface=password_authentication.DenialSurface.ENROLLMENT,
            )
            raise _http_from_refusal(exc) from exc
        if challenge.principal_type != "system_user":
            raise HTTPException(status_code=401, detail="Invalid MFA token")
        method = db.get(MFAMethod, coerce_uuid(method_id))
        if (
            not method
            or method.subscriber_id is not None
            or str(method.system_user_id) != str(challenge.principal_id)
        ):
            raise HTTPException(status_code=403, detail="MFA method not allowed")
        if method.method_type != MFAMethodType.totp:
            raise HTTPException(status_code=400, detail="Unsupported MFA method")
        ensure_mfa_not_locked(method)
        resolved_method_id = method.id
        phase_a_secret = method.secret or ""
        totp_ok = bool(
            pyotp.TOTP(_decrypt_secret(db, method.secret or "")).verify(
                code, valid_window=0
            )
        )
        failure_policy = _mfa_failure_policy(db)
        try:
            outcome = password_authentication.confirm_mfa_enrollment(
                db,
                challenge,
                request=request,
                method_id=resolved_method_id,
                expected_secret=phase_a_secret,
                totp_ok=totp_ok,
                failure_policy=failure_policy,
            )
        except password_authentication.PasswordAuthenticationError as exc:
            _record_denial(
                reason=_owner_denial_reason(exc),
                status_code=_owner_denial_status(exc),
                target=password_authentication.challenge_denial_target(db, challenge),
                request=request,
                phase=password_authentication.DenialPhase.OWNER,
                surface=password_authentication.DenialSurface.ENROLLMENT,
            )
            raise _http_from_refusal(exc) from exc
        return tokens_for_staged_session(outcome.staged_session), list(
            outcome.recovery_codes
        )

    @staticmethod
    def mfa_verify_response(db: Session, mfa_token: str, code: str, request: Request):
        result = AuthFlow.mfa_verify(db, mfa_token, code, request)
        if wants_refresh_in_body(request):
            return result
        return AuthFlow._response_with_refresh_cookie(
            db, result, TokenResponse, status.HTTP_200_OK
        )

    @staticmethod
    def refresh(db: Session, refresh_token: str, request: Request) -> TokenResponse:
        """Adapt an HTTP refresh request to the canonical session owner."""

        supplied_hash = _hash_token(refresh_token)
        command = auth_session_refresh.RefreshSessionCommand(
            context=CommandContext.system(
                actor="client:authentication-session",
                scope="authentication:session",
                reason="Renew an authentication session",
                idempotency_key=f"refresh:{supplied_hash}",
            ),
            refresh_token=refresh_token,
            client_ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            device_id=_clean_device_id(request.headers.get("x-device-id")),
        )
        db_session_adapter.release_read_transaction(db)
        try:
            outcome = auth_session_refresh.renew_authentication_session(
                db=db,
                command=command,
            )
        except auth_session_refresh.RefreshSessionError as exc:
            if exc.code == f"{auth_session_refresh.OWNER}.signing_unavailable":
                raise HTTPException(
                    status_code=503,
                    detail="Authentication service unavailable",
                ) from exc
            if exc.code == f"{auth_session_refresh.OWNER}.staff_projection_refused":
                logger.error("Staff refresh refused: %s", exc.code)
            raise HTTPException(status_code=401, detail=exc.message) from exc

        if outcome.disposition is auth_session_refresh.RefreshDisposition.EXPIRED:
            raise HTTPException(status_code=401, detail="Refresh token expired")
        if outcome.disposition is auth_session_refresh.RefreshDisposition.REUSE_REVOKED:
            raise HTTPException(status_code=401, detail="Refresh token reuse detected")

        if not outcome.access_token:
            raise RuntimeError("Accepted session renewal did not issue access")
        return TokenResponse(
            access_token=outcome.access_token,
            refresh_token=outcome.refresh_token,
        )

    @staticmethod
    def refresh_response(db: Session, refresh_token: str | None, request: Request):
        resolved = AuthFlow.resolve_refresh_token(request, refresh_token, None)
        if not resolved:
            raise HTTPException(status_code=401, detail="Missing refresh token")
        result = AuthFlow.refresh(
            db=db,
            refresh_token=resolved,
            request=request,
        )
        if wants_refresh_in_body(request):
            return result
        return AuthFlow._response_with_refresh_cookie(
            db, result, TokenResponse, status.HTTP_200_OK
        )

    @staticmethod
    def logout(db: Session, refresh_token: str, *, compensation: bool = False):
        from app.services import session_manager
        from app.services.db_session_adapter import db_session_adapter

        intent = (
            session_manager.RevocationIntent.COMPENSATE
            if compensation
            else session_manager.RevocationIntent.LOGOUT
        )
        with db_session_adapter.owner_command_session() as command_db:
            try:
                outcome = session_manager.revoke_authentication_sessions(
                    command_db,
                    session_manager.RevokeSessionsCommand(
                        context=CommandContext.system(
                            actor="vendor_admission"
                            if compensation
                            else "refresh_bearer",
                            scope="authentication_session",
                            reason=intent.value,
                        ),
                        intent=intent,
                        refresh_token=refresh_token,
                    ),
                )
            except session_manager.SessionRevocationError as exc:
                raise HTTPException(status_code=404, detail=exc.message) from exc
            except OperationalError as exc:
                if (
                    getattr(exc.orig, "sqlstate", None) == "55P03"
                    or getattr(exc.orig, "pgcode", None) == "55P03"
                ):
                    raise HTTPException(
                        status_code=503,
                        detail="Session revocation busy",
                        headers={"Retry-After": "5"},
                    ) from exc
                raise
        return {"revoked_at": outcome.revoked_at}

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
    def resolve_refresh_token(
        request: Request, refresh_token: str | None, db: Session | None = None
    ):
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
        *,
        staff_binding: staff_party_authentication.StaffSessionBinding | None = None,
    ) -> dict[str, str]:
        """Issue one session, retrying a transaction-level deadlock once.

        Legacy seam for callers that have already authenticated a principal by
        other means (``issue_session_tokens``, e.g. OIDC mobile federation):
        it stages and commits the session in its own transaction.

        The password and MFA paths no longer use it. Their credential-standing
        gate, session and audit are one owner transaction
        (``password_authentication``) built on ``stage_session_issue``, so the
        old assumption that credential evidence is committed before this
        boundary does not hold for them. For this seam, a deadlock rollback
        discards only the incomplete session and presence projection and the
        attempt can safely be replayed.
        """

        for attempt in range(2):
            try:
                active_staff_binding = staff_binding
                if staff_binding is not None:
                    staff_principal = (
                        staff_party_authentication.resolve_staff_principal_by_party(
                            db,
                            staff_binding.party_id,
                            staff_binding.system_user_id,
                            reference=staff_binding.system_user_id,
                        )
                    )
                    active_staff_binding = (
                        staff_party_authentication.StaffSessionBinding(
                            party_id=staff_binding.party_id,
                            system_user_id=staff_principal.id,
                        )
                    )
                return AuthFlow._issue_tokens_once(
                    db,
                    principal_type_or_principal_id,
                    principal_id_or_request,
                    request,
                    staff_binding=active_staff_binding,
                )
            except OperationalError as exc:
                # PostgreSQL rejects every subsequent statement until the
                # failed transaction is explicitly rolled back.
                db.rollback()
                sqlstate = getattr(exc.orig, "sqlstate", None)
                if sqlstate != "40P01" or attempt == 1:
                    raise
                logger.warning(
                    "auth_session_issue_deadlock_retry",
                    extra={
                        "event": "auth_session_issue_deadlock_retry",
                        "attempt": attempt + 2,
                    },
                )
        raise RuntimeError("unreachable session issuance retry state")

    @staticmethod
    def _issue_tokens_once(
        db: Session,
        principal_type_or_principal_id: str,
        principal_id_or_request: str | Request,
        request: Request | None = None,
        *,
        staff_binding: staff_party_authentication.StaffSessionBinding | None = None,
    ) -> dict[str, str]:
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

        staged = stage_session_issue(
            db,
            principal_type=principal_type,
            principal_id=principal_id,
            request=active_request,
            staff_binding=staff_binding,
        )
        db.commit()
        return tokens_for_staged_session(staged)


def stage_session_issue(
    db: Session,
    *,
    principal_type: str,
    principal_id: str,
    request: Request,
    staff_binding: staff_party_authentication.StaffSessionBinding | None = None,
    expires_at: datetime | None = None,
) -> StagedSession:
    """Typed compatibility adapter; session writes belong to session staging."""
    if principal_type not in {"system_user", "reseller_user", "subscriber"}:
        raise auth_session_staging.SessionStagingError()
    return auth_session_staging.stage_session_issue(
        db,
        command=auth_session_staging.StageSessionCommand(
            principal_type=cast(auth_session_staging.PrincipalType, principal_type),
            principal_id=coerce_uuid(principal_id),
            client=auth_session_staging.SessionClientEvidence(
                ip_address=client_ip(request),
                user_agent=_truncate_user_agent(request.headers.get("user-agent")),
                device_id=_clean_device_id(request.headers.get("x-device-id")),
            ),
            staff_binding=staff_binding,
            expires_at=expires_at,
        ),
    )


def tokens_for_staged_session(staged: StagedSession) -> dict[str, str]:
    """Encode only after the calling owner commits; no session access."""
    return auth_session_staging.tokens_for_staged_session(staged)


auth_flow = AuthFlow()


def issue_session_tokens(
    db: Session,
    *,
    principal_type: str,
    principal_id: str,
    request: Request,
    staff_binding: staff_party_authentication.StaffSessionBinding | None = None,
) -> dict[str, str]:
    """The PUBLIC name of the one session-issuance seam.

    ``AuthFlow._issue_tokens`` is and stays the only place a Sub session is
    minted. What was missing was a name another mechanism could call without
    reaching through a private attribute — which is how a second issuer gets
    written: not out of ambition, but because the first one had no door.

    This adds no policy of its own. Device supersession, refresh-token
    generation and hashing, the staff Party/context re-check, and the access
    token's claims all stay where they are; this only fixes the arguments in
    keyword form so a later parameter insertion cannot silently rebind them.

    A caller that has already AUTHENTICATED a principal by some other means
    (password, RADIUS, a verified external assertion) calls this. A caller that
    has not is looking for ``AuthFlow.login``.
    """

    return cast(
        dict[str, str],
        AuthFlow._issue_tokens(  # noqa: SLF001 - the owner's own public seam
            db,
            principal_type,
            principal_id,
            request,
            staff_binding=staff_binding,
        ),
    )


def change_password(
    db: Session,
    subscriber_id: str,
    current_password: str,
    new_password: str,
    *,
    current_session_id: str | None = None,
) -> datetime:
    """
    Change a user's password after verifying the current password.
    Revokes every other session for the principal and returns the timestamp
    when the password was changed.
    """
    principal_uuid = coerce_uuid(subscriber_id)
    stmt = (
        sa_select(UserCredential)
        .where(
            (UserCredential.subscriber_id == principal_uuid)
            | (UserCredential.system_user_id == principal_uuid)
            | (UserCredential.reseller_user_id == principal_uuid)
        )
        .where(UserCredential.provider == AuthProvider.local)
        .where(UserCredential.is_active.is_(True))
    )
    credential = db.scalars(stmt).first()

    if not credential:
        _record_denial(
            reason=password_authentication.DenialReason.INVALID_CREDENTIALS,
            target=None,
            phase=password_authentication.DenialPhase.STEP_UP,
            surface=password_authentication.DenialSurface.PASSWORD_CHANGE,
            status_code=404,
        )
        raise HTTPException(status_code=404, detail="No credentials found")

    # Snapshot the version with the hash being verified (same row read); the
    # write below is gated on it.
    expected_version = int(credential.credential_version)
    credential_id = credential.id
    if not verify_user_password(current_password, credential.password_hash):
        _record_denial(
            reason=password_authentication.DenialReason.INVALID_CREDENTIALS,
            target=password_authentication.DenialTarget(
                password_authentication.DenialTargetKind.CREDENTIAL, credential_id
            ),
            surface=password_authentication.DenialSurface.PASSWORD_CHANGE,
            phase=password_authentication.DenialPhase.STEP_UP,
        )
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    if current_password == new_password:
        raise HTTPException(status_code=400, detail="New password must be different")

    principal_type_for_policy = (
        "system_user"
        if credential.system_user_id is not None
        else "reseller_user"
        if getattr(credential, "reseller_user_id", None) is not None
        else "subscriber"
    )
    minimum = password_min_length_for(db, principal_type_for_policy)
    violations = password_policy_violations(new_password, minimum)
    if violations:
        raise HTTPException(status_code=400, detail=violations[0])

    if credential.system_user_id is not None:
        principal_type = "system_user"
        session_principal_filter = (
            AuthSession.system_user_id == credential.system_user_id
        )
    elif getattr(credential, "reseller_user_id", None) is not None:
        principal_type = "reseller_user"
        session_principal_filter = (
            AuthSession.reseller_user_id == credential.reseller_user_id
        )
    else:
        principal_type = "subscriber"
        session_principal_filter = AuthSession.subscriber_id == credential.subscriber_id
    portal_subscriber_id = (
        str(credential.subscriber_id) if credential.subscriber_id else None
    )
    new_hash = hash_password(new_password)

    # Phase B: gate on the credential version the current password was
    # verified against. A reset that committed since returns zero rows and the
    # change is refused (409) instead of silently overwriting the reset.
    try:
        outcome = password_authentication.apply_password_change(
            db,
            credential_id=credential_id,
            expected_version=expected_version,
            new_hash=new_hash,
            principal_type=principal_type,
            principal_id=str(principal_uuid),
            session_filter=session_principal_filter,
            current_session_id=(
                coerce_uuid(current_session_id) if current_session_id else None
            ),
        )
    except password_authentication.PasswordAuthenticationError as exc:
        _record_denial(
            reason=_owner_denial_reason(exc),
            status_code=_owner_denial_status(exc),
            target=password_authentication.DenialTarget(
                password_authentication.DenialTargetKind.CREDENTIAL, credential_id
            ),
            phase=password_authentication.DenialPhase.OWNER,
            surface=password_authentication.DenialSurface.PASSWORD_CHANGE,
        )
        raise _http_from_refusal(exc) from exc
    for revoked_session_id in outcome.revoked_session_ids:
        auth_cache.invalidate_session_context(
            revoked_session_id,
            principal_type=principal_type,
            principal_id=str(principal_uuid),
        )
    if principal_type == "subscriber" and portal_subscriber_id:
        _revoke_portal_sessions_for_subscriber(db, portal_subscriber_id)

    return outcome.changed_at


def _revoke_portal_sessions_for_subscriber(db: Session, subscriber_id: str) -> None:
    """Best-effort: drop Redis-backed customer/reseller web portal sessions too.

    `auth_sessions` revocation does not touch the opaque-token portal sessions,
    so a password change/reset would otherwise leave logged-in portal browsers
    untouched until their (sliding) TTL lapsed.
    """
    # Local imports: reseller_portal imports this module at import time.
    from app.services import customer_portal_session, reseller_portal

    try:
        customer_portal_session.revoke_customer_sessions_for_subscriber(
            subscriber_id, db=db
        )
        reseller_portal.revoke_reseller_sessions_for_subscriber(subscriber_id, db=db)
    except Exception:
        logger.warning(
            "Failed to revoke portal sessions for subscriber %s",
            subscriber_id,
            exc_info=True,
        )


def forgot_password_flow(
    db: Session, email: str, *, next_login_path: str | None = None
) -> None:
    """Deprecated compatibility adapter for durable password recovery."""

    from app.services import credential_recovery
    from app.services.owner_commands import CommandContext

    credential_recovery.request_password_recovery(
        db,
        credential_recovery.RequestPasswordRecoveryCommand(
            context=CommandContext.system(
                actor="service:legacy-auth-flow",
                scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
                reason="Legacy password recovery compatibility request",
            ),
            email=email,
            next_login_path=next_login_path,
        ),
    )


def _password_reset_result(capability) -> dict | None:
    if capability is None:
        return None
    return {
        "token": capability.token,
        "email": capability.email,
        "subscriber_name": capability.person_name,
        "principal_type": capability.principal_type,
        "principal_id": str(capability.principal_id),
        "ttl_minutes": capability.ttl_minutes,
    }


def request_password_reset(
    db: Session, email: str, *, ttl_minutes: int | None = None
) -> dict | None:
    """Deprecated token-only compatibility lookup for test and forced-reset code."""

    from app.services import credential_recovery

    return _password_reset_result(
        credential_recovery.issue_reset_capability_for_email(
            db,
            email,
            ttl_minutes=ttl_minutes,
        )
    )


def request_principal_password_reset(
    db: Session,
    *,
    principal_type: str,
    principal_id: UUID,
    ttl_minutes: int | None = None,
) -> dict | None:
    """Deprecated exact-capability compatibility wrapper."""

    from app.services import credential_recovery

    return _password_reset_result(
        credential_recovery.issue_exact_reset_capability(
            db,
            principal_type=principal_type,
            principal_id=principal_id,
            ttl_minutes=ttl_minutes,
        )
    )


def request_system_user_password_reset(
    db: Session,
    system_user_id: UUID,
    *,
    ttl_minutes: int | None = None,
) -> dict | None:
    """Deprecated exact staff-capability compatibility wrapper."""

    return request_principal_password_reset(
        db,
        principal_type="system_user",
        principal_id=system_user_id,
        ttl_minutes=ttl_minutes,
    )


def reset_password(db: Session, token: str, new_password: str) -> datetime:
    """Deprecated HTTP compatibility adapter for the contracted reset owner."""

    from app.services import credential_recovery
    from app.services.domain_errors import DomainError
    from app.services.owner_commands import CommandContext

    compatibility_db = Session(
        bind=db.connection(),
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        outcome = credential_recovery.complete_password_reset(
            compatibility_db,
            credential_recovery.CompletePasswordResetCommand(
                context=CommandContext.system(
                    actor="service:legacy-auth-flow",
                    scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
                    reason="Legacy password reset compatibility redemption",
                ),
                token=token,
                new_password=new_password,
            ),
        )
    except DomainError as exc:
        status_code = {
            "auth.credential_recovery.invalid_password": 400,
            "auth.credential_recovery.invalid_reset_capability": 401,
            "auth.credential_recovery.credential_not_found": 404,
        }.get(exc.code, 500)
        raise HTTPException(
            status_code=status_code,
            detail=exc.message.rstrip("."),
        ) from exc
    finally:
        compatibility_db.close()
        db.expire_all()
    return outcome.reset_at


def send_email_verification(db: Session, subscriber_id: str) -> bool:
    """
    Legacy mobile-profile compatibility path, pending a partial-profile owner.
    New request adapters must call email_verification.request_email_verification.

    Mint an email-verification token and send the verification email to the
    subscriber's address. No-op (returns False) when the subscriber is missing,
    has no email, or is already verified.

    Returns True if a verification email was dispatched.
    """
    from app.models.audit import AuditActorType
    from app.services.audit_adapter import record_audit_event
    from app.services.email import send_email_verification_email

    subscriber = cast(Subscriber | None, db.get(Subscriber, coerce_uuid(subscriber_id)))
    if not subscriber or not subscriber.email:
        return False
    if subscriber.email_verified:
        # Already verified: nothing to send.
        return False

    # Resolve the one authorised address before minting anything: a capability
    # that cannot be delivered to exactly one mailbox should not exist.
    recipient = resolve_capability_recipient(
        subscriber.email, subject=f"subscriber:{subscriber.id}"
    )
    ttl_minutes = _email_verification_ttl_minutes(db)
    token = _issue_email_verification_token(
        db,
        str(subscriber.id),
        recipient,
        ttl_minutes=ttl_minutes,
    )
    record_audit_event(
        db,
        action="auth.email_verification_requested",
        entity_type="subscriber",
        entity_id=str(subscriber.id),
        actor_type=AuditActorType.user,
        actor_id=str(subscriber.id),
        metadata={"email": recipient},
    )
    return send_email_verification_email(
        db=db,
        to_email=recipient,
        verification_token=token,
        person_name=subscriber.display_name or subscriber.first_name,
        expires_minutes=ttl_minutes,
    )


def set_subscriber_email(
    db: Session, subscriber_id: str, new_email: str | None
) -> bool:
    """Add or change a subscriber's email, re-arming verification.

    Single source of truth shared by the web profile form and the mobile ``/me``
    update so the rule "a changed/added email must be re-verified, with a fresh
    link dispatched" lives in exactly one place. No-op (returns ``False``) when
    the email is blank or unchanged. Returns ``True`` when the email changed (and
    a verification email was dispatched). Email is non-unique contact info, so
    sharing an address with another subscriber is allowed and not blocked.
    """
    new_email = (new_email or "").strip()
    if not new_email:
        return False
    subscriber = cast(Subscriber | None, db.get(Subscriber, coerce_uuid(subscriber_id)))
    if subscriber is None:
        return False
    if new_email.lower() == (subscriber.email or "").strip().lower():
        return False

    from app.schemas.subscriber import SubscriberUpdate
    from app.services import subscriber as subscriber_service

    # Route through the subscriber service so the unique constraint and the
    # customer identity-resolution index are maintained the same way the web
    # and admin edit paths do.
    subscriber_service.subscribers.update(
        db,
        str(subscriber.id),
        SubscriberUpdate(email=new_email, email_verified=False),
    )
    try:
        send_email_verification(db, str(subscriber.id))
    except Exception:
        logger.warning(
            "verification email after email change failed for %s",
            subscriber_id,
            exc_info=True,
        )
    return True


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
        # Party is the identity key. `system_user_id` is compared afterwards as
        # the Sub-owned staff-context assertion; a missing projection refuses.
        # Returning None is the fail-closed answer, so the caller learns no
        # projection detail.
        try:
            principal = staff_party_authentication.resolve_staff_session_principal(
                db,
                party_id=session.party_id,
                system_user_id=session.system_user_id,
                reference=str(session.id),
            )
        except staff_party_authentication.StaffProjectionError as exc:
            logger.error(
                "Staff session refused: %s (system_user=%s)",
                exc.refusal.value,
                exc.credential_id,
            )
            return None
    else:
        principal = db.get(Subscriber, active_id)
    if not principal:
        return None
    if principal_type == "system_user" and not getattr(principal, "is_active", False):
        return None

    return session, principal, principal_type
