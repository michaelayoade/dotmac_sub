"""Shared, transport-neutral customer password-step decisions.

Browser and native adapters deliver the outcome through their own session
transport. Credential standing, sessions and denial writes remain owned by
password_authentication.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.auth import UserCredential
from app.models.catalog import AccessCredential
from app.models.domain_settings import SettingDomain
from app.models.radius import RadiusUser
from app.models.subscriber import Subscriber, SubscriberStatus, UserType
from app.services import auth_flow as auth_flow_service
from app.services import customer_login_identity, radius_auth
from app.services import password_authentication as pa
from app.services.access_credential_secret import is_secret_reference_value
from app.services.auth_flow import verify_user_password
from app.services.domain_errors import DomainError
from app.services.rate_limiter_adapter import allow_operation
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


class CustomerLoginAudience(StrEnum):
    BROWSER = "general"
    NATIVE_CUSTOMER = "customer"


class CustomerLoginFailure(StrEnum):
    INVALID_CREDENTIALS = "invalid_credentials"
    LOCKED = "locked"
    RESET_REQUIRED = "must_change_password"
    DISABLED = "account_disabled"
    NOT_CUSTOMER = "customer_required"


class CustomerLoginError(DomainError):
    def __init__(self, kind: CustomerLoginFailure, message: str):
        self.kind = kind
        super().__init__(
            code=f"auth.customer_authentication.{kind.value}",
            message=message,
            retryable=False,
        )


@dataclass(frozen=True, slots=True)
class CustomerLoginEvidence:
    correlation_id: UUID
    trusted_ip: IPv4Address | IPv6Address | None
    surface: pa.DenialSurface
    # Adapter-supplied response metadata keeps audit status aligned with the
    # native JSON or browser form refusal without owning HTTP mapping here.
    eligibility_denial_status: Literal[401, 403] = 401
    reset_denial_status: Literal[401, 428] = 401

    def __post_init__(self) -> None:
        if not isinstance(self.correlation_id, UUID) or not isinstance(
            self.surface, pa.DenialSurface
        ):
            raise TypeError(
                "Customer login evidence requires a UUID and bounded surface"
            )
        if self.trusted_ip is not None and not isinstance(
            self.trusted_ip, (IPv4Address, IPv6Address)
        ):
            raise TypeError("Customer login evidence requires a normalized IP")


@dataclass(frozen=True, slots=True)
class PrepareCustomerLogin:
    identifier: str = field(repr=False)
    password: str = field(repr=False)
    evidence: CustomerLoginEvidence
    audience: CustomerLoginAudience

    def __post_init__(self) -> None:
        if not isinstance(self.audience, CustomerLoginAudience):
            raise TypeError("Customer login requires a bounded audience")


@dataclass(frozen=True, slots=True)
class PreparedCustomerLogin:
    identifier: str = field(repr=False)
    subscriber_id: UUID
    subscription_id: UUID | None
    verified: pa.VerifiedCredential = field(repr=False)
    denial_target: pa.DenialTarget | None


def _setting_int(db: Session, domain: SettingDomain, key: str, default: int) -> int:
    raw = resolve_value(db, domain, key)
    try:
        return int(str(raw)) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _portal_denial(
    request: CustomerLoginEvidence,
    *,
    reason: pa.DenialReason,
    target: pa.DenialTarget | None,
    effect: pa.PasswordFailureEffect | None = None,
) -> None:
    status_code: Literal[401, 403, 428] = 401
    if reason in {
        pa.DenialReason.LOCKED,
        pa.DenialReason.DISABLED,
        pa.DenialReason.CUSTOMER_REQUIRED,
    }:
        status_code = request.eligibility_denial_status
    elif reason is pa.DenialReason.RESET_REQUIRED:
        status_code = request.reset_denial_status
    pa.record_authentication_denial(
        pa.AuthenticationDenial(
            reason=reason,
            surface=request.surface,
            phase=pa.DenialPhase.PASSWORD,
            target=target,
            attempt_id=uuid4(),
            correlation_id=request.correlation_id,
            trusted_ip=request.trusted_ip,
            effect=effect,
            status_code=status_code,
        )
    )
    logger.info(
        "customer_login_denied",
        extra={
            "request_id": str(request.correlation_id),
            "reason": reason.value,
            "surface": request.surface.value,
            "target_kind": target.kind.value if target is not None else "unknown",
        },
    )


def _record_customer_local_login_failure(
    db: Session,
    credential: UserCredential,
    *,
    request: CustomerLoginEvidence,
) -> None:
    policy = pa.FailurePolicy(
        max_attempts=_setting_int(
            db, SettingDomain.auth, "customer_login_max_attempts", 5
        ),
        lock_minutes=_setting_int(
            db, SettingDomain.auth, "customer_lockout_minutes", 15
        ),
    )
    _portal_denial(
        request,
        reason=pa.DenialReason.INVALID_CREDENTIALS,
        target=pa.DenialTarget(pa.DenialTargetKind.CREDENTIAL, credential.id),
        effect=pa.PasswordFailureEffect(credential.id, policy),
    )


def _primary_customer_totp_enabled(db: Session, subscriber_id: UUID) -> bool:
    return (
        auth_flow_service._primary_totp_method(db, "subscriber", str(subscriber_id))
        is not None
    )


def prepare_customer_login(
    db: Session, query: PrepareCustomerLogin
) -> PreparedCustomerLogin:
    username, password, request = query.identifier, query.password, query.evidence
    denial_target: pa.DenialTarget | None = None
    normalized_username = username.strip()
    if not normalized_username:
        _portal_denial(request, reason=pa.DenialReason.INVALID_CREDENTIALS, target=None)
        raise CustomerLoginError(
            CustomerLoginFailure.INVALID_CREDENTIALS, "Username is required"
        )

    account_id = None
    subscriber_id = None
    subscription_id = None
    authenticated_locally = False
    local_password_failed = False
    local_login_is_email_alias = False
    verified: pa.VerifiedCredential | None = None
    access_credential: AccessCredential | pa.PortalAccessSnapshot | None = None
    radius_snapshot: pa.PortalRadiusSnapshot | None = None

    local_resolution = customer_login_identity.resolve_customer_login_identity(
        db,
        customer_login_identity.ResolveCustomerLoginIdentity(
            identifier=normalized_username
        ),
    )
    refusal = customer_login_identity.resolution_error(local_resolution)
    if refusal is not None:
        _portal_denial(request, reason=pa.DenialReason.INVALID_CREDENTIALS, target=None)
        raise refusal
    local_credential = (
        db.get(UserCredential, local_resolution.credential_id)
        if local_resolution.status
        is customer_login_identity.CustomerLoginResolutionStatus.matched
        and local_resolution.credential_id is not None
        else None
    )
    local_login_is_email_alias = (
        local_credential is not None and "@" in normalized_username
    )
    if local_credential:
        denial_target = pa.DenialTarget(
            pa.DenialTargetKind.CREDENTIAL, local_credential.id
        )
        now = datetime.now(UTC)
        locked_until = local_credential.locked_until
        if locked_until and locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=UTC)
        if locked_until and locked_until > now:
            _portal_denial(request, reason=pa.DenialReason.LOCKED, target=denial_target)
            raise CustomerLoginError(
                CustomerLoginFailure.LOCKED,
                auth_flow_service.lockout_detail(
                    "Account locked", locked_until=locked_until
                ),
            )
        if not verify_user_password(password, local_credential.password_hash):
            local_password_failed = True
        else:
            if local_credential.must_change_password:
                _portal_denial(
                    request,
                    reason=pa.DenialReason.RESET_REQUIRED,
                    target=denial_target,
                )
                raise CustomerLoginError(
                    CustomerLoginFailure.RESET_REQUIRED,
                    "Password reset required. Please contact support.",
                )

            authenticated_locally = True
            subscriber_id = local_credential.subscriber_id
            account_id = subscriber_id
            verified = pa.VerifiedCredential(
                src=pa.SRC_USER_CREDENTIAL,
                principal_type="subscriber",
                principal_id=str(subscriber_id),
                audience="general",
                staff_binding=None,
                mfa_required=False,
                enrollment_required=False,
                credential_id=local_credential.id,
                credential_version=local_credential.credential_version,
                provider=local_credential.provider,
                verified_hash=local_credential.password_hash,
            )

    if not authenticated_locally:

        def raise_invalid_login() -> None:
            if local_credential and local_password_failed:
                _record_customer_local_login_failure(
                    db, local_credential, request=request
                )
            else:
                _portal_denial(
                    request,
                    reason=pa.DenialReason.INVALID_CREDENTIALS,
                    target=denial_target,
                )
            raise CustomerLoginError(
                CustomerLoginFailure.INVALID_CREDENTIALS, "Invalid username or password"
            )

        if local_password_failed and local_login_is_email_alias:
            raise_invalid_login()

        # The RADIUS/PPPoE path has no per-credential lockout columns, so
        # throttle total attempts per username instead (in-memory,
        # per-worker — a backstop against online brute force, not a
        # substitute for the DB-backed local-credential lockout).
        decision = allow_operation(
            f"portal:radius-login:{normalized_username.lower()}",
            limit=10,
            window_seconds=900,
        )
        if not decision.allowed:
            _portal_denial(request, reason=pa.DenialReason.LOCKED, target=denial_target)
            raise CustomerLoginError(
                CustomerLoginFailure.LOCKED,
                auth_flow_service.lockout_detail(
                    "Account locked",
                    retry_after_seconds=decision.retry_after_seconds,
                ),
            )

        # Try RADIUS server authentication first
        radius_authenticated = False
        sources = pa.resolve_portal_authentication_sources(
            db, pa.PortalAuthenticationSourceQuery(identifier=normalized_username)
        )
        radius_snapshot = sources.radius
        access_credential = sources.access
        # Snapshot BEFORE network verification; an ORM refresh cannot adopt
        # post-verification source standing into this immutable binding.
        radius_binding = (
            (
                radius_snapshot.id,
                radius_snapshot.updated_at,
                access_credential.id if access_credential else None,
                access_credential.updated_at if access_credential else None,
            )
            if radius_snapshot is not None
            else None
        )
        if radius_snapshot is not None:
            denial_target = pa.DenialTarget(
                pa.DenialTargetKind.RADIUS, radius_snapshot.id
            )
        elif access_credential is not None:
            denial_target = pa.DenialTarget(
                pa.DenialTargetKind.ACCESS, access_credential.id
            )
        access_binding = (
            (
                access_credential.id,
                access_credential.updated_at,
                access_credential.secret_hash,
            )
            if access_credential
            else None
        )
        radius_authenticated = (
            radius_auth.observe_authentication(
                db=db,
                query=radius_auth.ObserveRadiusAuthentication(
                    username=normalized_username, password=password
                ),
            )
            is radius_auth.RadiusAuthenticationObservation.ACCEPTED
        )
        if not radius_authenticated:
            logger.debug("portal_radius_verification_failed")

        if radius_authenticated:
            radius_user = (
                db.query(RadiusUser)
                .filter(RadiusUser.username == normalized_username)
                .filter(RadiusUser.is_active.is_(True))
                .first()
            )

            if radius_user:
                account_id = radius_user.subscriber_id
                subscription_id = radius_user.subscription_id
                subscriber_id = radius_user.subscriber_id
                if radius_user.subscription_id and not subscriber_id:
                    from app.models.catalog import Subscription

                    subscription = db.get(Subscription, radius_user.subscription_id)
                    if subscription and subscription.subscriber_id:
                        subscriber_id = subscription.subscriber_id
                if account_id and not subscriber_id:
                    account = db.get(Subscriber, account_id)
                    if account:
                        subscriber_id = account.id
            else:
                credential = (
                    db.query(AccessCredential)
                    .filter(AccessCredential.username == normalized_username)
                    .filter(AccessCredential.is_active.is_(True))
                    .first()
                )
                if credential:
                    access_credential = credential
                    account_id = credential.subscriber_id
                    subscriber_id = credential.subscriber_id
                if account_id and not subscriber_id:
                    account = db.get(Subscriber, account_id)
                    if account:
                        subscriber_id = account.id
        else:
            # Fallback: authenticate directly against PPPoE/access credentials.
            # This allows portal login even when the RADIUS server is unreachable
            # or the password format isn't compatible with RADIUS (e.g. migrated hashes).
            credential = (
                db.query(AccessCredential)
                .filter(AccessCredential.username == normalized_username)
                .filter(AccessCredential.is_active.is_(True))
                .first()
            )
            if credential is not None:
                denial_target = pa.DenialTarget(
                    pa.DenialTargetKind.ACCESS, credential.id
                )
            if (
                credential
                and credential.secret_hash
                and not is_secret_reference_value(credential.secret_hash)
            ):
                from app.services.credential_crypto import decrypt_credential

                stored_password = decrypt_credential(credential.secret_hash)
                if stored_password and secrets.compare_digest(
                    stored_password, password
                ):
                    access_credential = credential
                    account_id = credential.subscriber_id
                    subscriber_id = credential.subscriber_id
                    logger.info("portal_access_credential_verified")
                else:
                    raise_invalid_login()
            else:
                raise_invalid_login()

    if not account_id or not subscriber_id:
        _portal_denial(request, reason=pa.DenialReason.DISABLED, target=denial_target)
        raise CustomerLoginError(
            CustomerLoginFailure.DISABLED,
            "Customer account not found. Please contact support.",
        )

    # Status gate for every auth path (local, RADIUS, access-credential
    # fallback): canceled subscribers are gone, disabled ones are told so.
    # Suspended/blocked/delinquent stay able to log in and pay.
    portal_subscriber = db.get(Subscriber, subscriber_id)
    if not portal_subscriber or portal_subscriber.status == SubscriberStatus.canceled:
        _portal_denial(request, reason=pa.DenialReason.DISABLED, target=denial_target)
        raise CustomerLoginError(
            CustomerLoginFailure.DISABLED,
            "Customer account not found. Please contact support.",
        )
    if portal_subscriber.status == SubscriberStatus.disabled:
        _portal_denial(request, reason=pa.DenialReason.DISABLED, target=denial_target)
        raise CustomerLoginError(
            CustomerLoginFailure.DISABLED, "Account disabled. Please contact support."
        )

    mfa_required = _primary_customer_totp_enabled(db, subscriber_id)
    if verified is None:
        if radius_authenticated and radius_binding:
            rid, ru, acid, acu = radius_binding
            verified = pa.VerifiedCredential(
                src=pa.SRC_RADIUS_USER,
                principal_type="subscriber",
                principal_id=str(subscriber_id),
                audience="general",
                staff_binding=None,
                mfa_required=mfa_required,
                enrollment_required=False,
                access_credential_id=acid,
                access_credential_updated_at=acu,
                radius_user_id=rid,
                radius_user_updated_at=ru,
            )
        elif access_credential:
            if radius_authenticated and access_binding is None:
                _portal_denial(
                    request,
                    reason=pa.DenialReason.CREDENTIAL_CHANGED,
                    target=denial_target,
                )
                raise CustomerLoginError(
                    CustomerLoginFailure.INVALID_CREDENTIALS,
                    "Invalid username or password",
                )
            if radius_authenticated and not auth_flow_service.verify_service_secret(
                password, access_binding[2] if access_binding else None
            ):
                _portal_denial(
                    request,
                    reason=pa.DenialReason.CREDENTIAL_CHANGED,
                    target=denial_target,
                )
                raise CustomerLoginError(
                    CustomerLoginFailure.INVALID_CREDENTIALS,
                    "Invalid username or password",
                )
            access_id, access_updated_at, access_hash = (
                access_binding
                if radius_authenticated and access_binding
                else (
                    access_credential.id,
                    access_credential.updated_at,
                    access_credential.secret_hash,
                )
            )
            verified = pa.VerifiedCredential(
                src=pa.SRC_ACCESS_CREDENTIAL,
                principal_type="subscriber",
                principal_id=str(subscriber_id),
                audience="general",
                staff_binding=None,
                mfa_required=mfa_required,
                enrollment_required=False,
                verified_hash=access_hash,
                access_credential_id=access_id,
                access_credential_updated_at=access_updated_at,
            )
        else:
            raise CustomerLoginError(
                CustomerLoginFailure.INVALID_CREDENTIALS, "Invalid username or password"
            )
    else:
        verified = replace(verified, mfa_required=mfa_required)

    if (
        query.audience is CustomerLoginAudience.NATIVE_CUSTOMER
        and portal_subscriber.user_type is not UserType.customer
    ):
        _portal_denial(
            request, reason=pa.DenialReason.CUSTOMER_REQUIRED, target=denial_target
        )
        raise CustomerLoginError(
            CustomerLoginFailure.NOT_CUSTOMER,
            "Only customer accounts can sign in to this app.",
        )
    verified = replace(verified, audience=query.audience.value)
    return PreparedCustomerLogin(
        normalized_username,
        UUID(str(subscriber_id)),
        UUID(str(subscription_id)) if subscription_id is not None else None,
        verified,
        denial_target,
    )
