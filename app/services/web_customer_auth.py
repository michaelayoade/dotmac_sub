"""Service helpers for customer portal auth."""

import html
import logging
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pyotp
from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jose import JWTError
from sqlalchemy.orm import Session

from app.csrf import renew_csrf_cookie

logger = logging.getLogger(__name__)

from app.models.auth import MFAMethod, UserCredential
from app.models.catalog import AccessCredential
from app.models.domain_settings import SettingDomain
from app.models.radius import RadiusUser
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import (
    anonymous_recovery_adapter,
    credential_recovery,
    customer_credential_enrollment,
    customer_login_identity,
    customer_portal,
    radius_auth,
)
from app.services import auth_flow as auth_flow_service
from app.services import module_manager as module_manager_service
from app.services import (
    password_authentication as pa,
)
from app.services.auth_flow import StagedSession, verify_user_password
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.rate_limiter_adapter import allow_operation
from app.services.settings_spec import resolve_value
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
_CUSTOMER_MFA_TOKEN_COOKIE = "customer_mfa_pending"
_CUSTOMER_MFA_CONTEXT_COOKIE = "customer_mfa_context"
_CUSTOMER_MFA_MAX_AGE = 300
_CUSTOMER_RESET_LOGIN_PATH = "/portal/auth/login?next=/portal/dashboard"


def _credential_recovery_context(reason: str) -> CommandContext:
    return CommandContext.system(
        actor="service:customer-auth-web",
        scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
        reason=reason,
    )


def _safe_next(next_url: str | None, fallback: str = "/portal/dashboard") -> str:
    """Validate redirect URL to prevent open redirect attacks."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return fallback


def _setting_int(db: Session, domain: SettingDomain, key: str, default: int) -> int:
    raw = resolve_value(db, domain, key)
    if raw is None:
        return default
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


def _record_customer_local_login_failure(
    db: Session, credential: UserCredential, *, now: datetime | None = None
) -> None:
    policy = pa.FailurePolicy(
        max_attempts=_setting_int(
            db, SettingDomain.auth, "customer_login_max_attempts", 5
        ),
        lock_minutes=_setting_int(
            db, SettingDomain.auth, "customer_lockout_minutes", 15
        ),
    )
    pa.record_password_failure(
        db,
        credential.id,
        policy,
        principal_type="subscriber",
        principal_id=str(credential.subscriber_id),
    )


def _customer_mfa_context_token(
    db: Session,
    *,
    username: str,
    account_id: object,
    subscriber_id: object,
    subscription_id: object | None,
    remember: bool,
    next_url: str | None,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "typ": "customer_mfa_context",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=_CUSTOMER_MFA_MAX_AGE)).timestamp()),
        "username": username,
        "account_id": str(account_id),
        "subscriber_id": str(subscriber_id),
        "subscription_id": str(subscription_id) if subscription_id else None,
        "remember": bool(remember),
        "next": _safe_next(next_url),
    }
    return auth_flow_service._jwt_encode_token(  # noqa: SLF001
        payload,
        auth_flow_service._jwt_secret(db),  # noqa: SLF001
        auth_flow_service._jwt_algorithm(db),  # noqa: SLF001
    )


def _decode_customer_mfa_context(db: Session, token: str) -> dict:
    try:
        payload = auth_flow_service._jwt_decode_token(  # noqa: SLF001
            token,
            auth_flow_service._jwt_secret(db),  # noqa: SLF001
            auth_flow_service._jwt_algorithm(db),  # noqa: SLF001
        )
    except JWTError as exc:
        raise ValueError("Invalid MFA session") from exc
    if payload.get("typ") != "customer_mfa_context":
        raise ValueError("Invalid MFA session")
    return payload


def _set_customer_mfa_cookies(
    response: Response,
    *,
    mfa_token: str,
    context_token: str,
) -> None:
    for key, value in (
        (_CUSTOMER_MFA_TOKEN_COOKIE, mfa_token),
        (_CUSTOMER_MFA_CONTEXT_COOKIE, context_token),
    ):
        response.set_cookie(
            key=key,
            value=value,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=_CUSTOMER_MFA_MAX_AGE,
        )


def _clear_customer_mfa_cookies(response: Response) -> None:
    response.delete_cookie(_CUSTOMER_MFA_TOKEN_COOKIE)
    response.delete_cookie(_CUSTOMER_MFA_CONTEXT_COOKIE)


def list_active_mfa_methods(db: Session, subscriber_id: object) -> list[MFAMethod]:
    """Active MFA methods for a subscriber, newest first (profile page)."""
    return (
        db.query(MFAMethod)
        .filter(MFAMethod.subscriber_id == subscriber_id)
        .filter(MFAMethod.is_active.is_(True))
        .order_by(MFAMethod.created_at.desc())
        .all()
    )


def _primary_customer_totp_enabled(db: Session, subscriber_id: object) -> bool:
    return (
        auth_flow_service._primary_totp_method(  # noqa: SLF001
            db, "subscriber", str(subscriber_id)
        )
        is not None
    )


def _verify_customer_mfa_token(
    db: Session,
    *,
    mfa_token: str,
    context: dict,
    code: str,
    request: Request,
) -> StagedSession:
    challenge = pa.decode_challenge(db, mfa_token, expected_typ="mfa")
    subscriber_id = str(context.get("subscriber_id") or "")
    if (
        challenge.principal_type != "subscriber"
        or challenge.audience != "general"
        or challenge.principal_id != subscriber_id
        or str(context.get("account_id") or "") != subscriber_id
    ):
        raise ValueError("Invalid MFA session")
    method = auth_flow_service._primary_totp_method(db, "subscriber", subscriber_id)  # noqa: SLF001
    if not method:
        raise ValueError("MFA method not found")
    auth_flow_service.ensure_mfa_not_locked(method)
    secret = auth_flow_service._decrypt_secret(db, method.secret or "")  # noqa: SLF001
    totp_ok = pyotp.TOTP(secret).verify(code.strip(), valid_window=0)
    return pa.complete_mfa(
        db,
        challenge,
        request=request,
        method_id=method.id,
        totp_ok=totp_ok,
        recovery_code_hash=None,
        failure_policy=auth_flow_service._mfa_failure_policy(db),  # noqa: SLF001
        session_sink="customer_portal",
        expires_at=_portal_expiry(db, bool(context.get("remember"))),
    )


def _portal_expiry(db: Session, remember: bool) -> datetime:
    ttl = (
        customer_portal.get_remember_max_age(db)
        if remember
        else customer_portal.get_session_max_age(db)
    )
    return datetime.now(UTC) + timedelta(seconds=ttl)


def _deliver_customer_session(
    db: Session,
    *,
    staged: StagedSession,
    username: str,
    account_id: UUID | str,
    subscriber_id: UUID | str,
    subscription_id: UUID | str | None,
    remember: bool,
) -> str:
    try:
        return customer_portal.create_customer_session(
            username=username,
            account_id=account_id,
            subscriber_id=subscriber_id,
            subscription_id=subscription_id,
            remember=remember,
            db=db,
            auth_session_id=UUID(staged.session_id),
        )
    except Exception as exc:
        try:
            pa.revoke_undelivered_customer_session(
                db,
                session_id=UUID(staged.session_id),
                subscriber_id=UUID(str(subscriber_id)),
            )
        except Exception:
            logger.warning("undelivered_customer_session_revoke_failed", exc_info=True)
        raise RuntimeError("Customer session delivery unavailable") from exc


def get_current_customer_from_request(request: Request, db: Session) -> dict | None:
    cached_customer = getattr(request.state, "customer", None)
    if isinstance(cached_customer, dict):
        return cached_customer

    session_token = request.cookies.get(customer_portal.SESSION_COOKIE_NAME)
    customer = customer_portal.get_current_customer(session_token, db)
    if not customer:
        return None
    enriched = dict(customer)
    try:
        enriched["module_states"] = module_manager_service.load_module_states(db)
        enriched["feature_states"] = module_manager_service.load_feature_states(db)
    except Exception:
        enriched["module_states"] = {}
        enriched["feature_states"] = {}
    request.state.customer = enriched
    return enriched


def customer_login_page(
    request: Request,
    db: Session,
    error: str | None = None,
    next_url: str | None = None,
):
    customer = get_current_customer_from_request(request, db)
    if customer:
        return RedirectResponse(url=_safe_next(next_url), status_code=303)

    return templates.TemplateResponse(
        "customer/auth/login.html",
        {"request": request, "error": error, "next": next_url},
    )


def customer_forgot_password_page(request: Request, db: Session, success: bool = False):
    customer = get_current_customer_from_request(request, db)
    if customer:
        return RedirectResponse(url=_safe_next(None), status_code=303)
    return templates.TemplateResponse(
        "customer/auth/forgot-password.html",
        {"request": request, "success": success},
    )


def customer_forgot_password_submit(request: Request, db: Session, email: str):
    anonymous_recovery_adapter.request_password_recovery(
        command=credential_recovery.RequestPasswordRecoveryCommand(
            context=_credential_recovery_context(
                "Customer portal password recovery request"
            ),
            email=email,
            next_login_path=_CUSTOMER_RESET_LOGIN_PATH,
        ),
    )
    return templates.TemplateResponse(
        "customer/auth/forgot-password.html",
        {"request": request, "success": True},
    )


def customer_credential_enrollment_page(
    request: Request,
    db: Session,
    token: str,
    *,
    error: str | None = None,
    username: str = "",
    status_code: int = 200,
):
    """Render the self-care credential form without consuming its capability."""

    return templates.TemplateResponse(
        request,
        "customer/auth/credential-enrollment.html",
        {
            "token": token,
            "username": username,
            "error": error,
            "password_min_length": auth_flow_service.password_min_length(db),
        },
        status_code=status_code,
    )


def customer_credential_enrollment_submit(
    request: Request,
    db: Session,
    *,
    token: str,
    password: str,
    password_confirm: str,
    username: str = "",
):
    """Translate the self-care form into the canonical enrollment command."""

    if password != password_confirm:
        return customer_credential_enrollment_page(
            request,
            db,
            token,
            error="Passwords do not match",
            username=username,
            status_code=400,
        )
    try:
        db_session_adapter.release_read_transaction(db)
        customer_credential_enrollment.complete_referral_enrollment(
            db,
            customer_credential_enrollment.CompleteReferralEnrollmentCommand(
                context=CommandContext.system(
                    actor="public:customer-credential-enrollment-form",
                    scope=customer_credential_enrollment.CUSTOMER_CREDENTIAL_ENROLLMENT_SCOPE,
                    reason=(
                        "Redeem referral customer credential enrollment capability"
                    ),
                ),
                token=token,
                new_password=password,
                username=username or None,
            ),
        )
    except DomainError as exc:
        status_code = {
            "auth.customer_credential_enrollment.invalid_password": (
                status.HTTP_400_BAD_REQUEST
            ),
            "auth.customer_credential_enrollment.inactive_account": (
                status.HTTP_409_CONFLICT
            ),
            "auth.customer_credential_enrollment.username_unavailable": (
                status.HTTP_409_CONFLICT
            ),
            "auth.customer_credential_enrollment.invalid_username": (
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
        }.get(exc.code, status.HTTP_401_UNAUTHORIZED)
        error = (
            exc.message
            if status_code in {400, 409, 422}
            else "This enrollment link is invalid, expired, or already used."
        )
        return customer_credential_enrollment_page(
            request,
            db,
            token,
            error=error,
            username=username,
            status_code=status_code,
        )
    return RedirectResponse(
        url="/portal/auth/login?enrollment=success",
        status_code=303,
    )


def customer_verify_email_page(request: Request, db: Session, token: str):
    """Complete the capability in a fresh owner; retain the existing GET route."""
    from app.services import email_verification

    error_msg = "This verification link is invalid or has expired."
    response_status = 400
    headers = None
    try:
        with db_session_adapter.owner_command_session() as owner_db:
            email_verification.complete_email_verification(
                owner_db,
                command=email_verification.CompleteEmailVerificationCommand(
                    context=CommandContext.system(
                        actor="capability:email-verification",
                        scope=email_verification.EMAIL_VERIFICATION_SCOPE,
                        reason="Customer email capability completion",
                    ),
                    token=token,
                ),
            )
        return templates.TemplateResponse(
            "customer/auth/verify-email.html",
            {"request": request, "success": True},
        )
    except DomainError as exc:
        if exc.code != "auth.email_verification.invalid_capability":
            response_status = 503
    except Exception:
        response_status = 503
    if response_status == 503:
        error_msg = "Email verification is temporarily unavailable. Please try again."
        headers = {"Retry-After": "5"}
    return templates.TemplateResponse(
        "customer/auth/verify-email.html",
        {"request": request, "success": False, "error": error_msg},
        status_code=response_status,
        headers=headers,
    )


def customer_login_submit(
    request: Request,
    db: Session,
    username: str,
    password: str,
    remember: bool,
    next_url: str | None,
):
    try:
        normalized_username = username.strip()
        if not normalized_username:
            raise ValueError("Username is required")

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
            now = datetime.now(UTC)
            locked_until = local_credential.locked_until
            if locked_until and locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=UTC)
            if locked_until and locked_until > now:
                raise ValueError(
                    auth_flow_service.lockout_detail(
                        "Account locked", locked_until=locked_until
                    )
                )
            if not verify_user_password(password, local_credential.password_hash):
                local_password_failed = True
            else:
                if local_credential.must_change_password:
                    raise ValueError("Password reset required. Please contact support.")

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
                    _record_customer_local_login_failure(db, local_credential)
                raise ValueError("Invalid username or password")

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
                raise ValueError(
                    auth_flow_service.lockout_detail(
                        "Account locked",
                        retry_after_seconds=decision.retry_after_seconds,
                    )
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
            access_binding = (
                (
                    access_credential.id,
                    access_credential.updated_at,
                    access_credential.secret_hash,
                )
                if access_credential
                else None
            )
            try:
                radius_auth.authenticate(
                    db=db, username=normalized_username, password=password
                )
                radius_authenticated = True
            except Exception:
                logger.debug(
                    "RADIUS auth failed for %s, trying access credential",
                    normalized_username,
                )

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
                if credential and credential.secret_hash:
                    from app.services.credential_crypto import decrypt_credential

                    stored_password = decrypt_credential(credential.secret_hash)
                    if stored_password and secrets.compare_digest(
                        stored_password, password
                    ):
                        access_credential = credential
                        account_id = credential.subscriber_id
                        subscriber_id = credential.subscriber_id
                        logger.info(
                            "Portal login via access credential for %s",
                            normalized_username,
                        )
                    else:
                        raise_invalid_login()
                else:
                    raise_invalid_login()

        if not account_id or not subscriber_id:
            raise ValueError("Customer account not found. Please contact support.")

        # Status gate for every auth path (local, RADIUS, access-credential
        # fallback): canceled subscribers are gone, disabled ones are told so.
        # Suspended/blocked/delinquent stay able to log in and pay.
        portal_subscriber = db.get(Subscriber, subscriber_id)
        if (
            not portal_subscriber
            or portal_subscriber.status == SubscriberStatus.canceled
        ):
            raise ValueError("Customer account not found. Please contact support.")
        if portal_subscriber.status == SubscriberStatus.disabled:
            raise ValueError("Account disabled. Please contact support.")

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
                    raise ValueError("Invalid username or password")
                if radius_authenticated and not auth_flow_service.verify_service_secret(
                    password, access_binding[2] if access_binding else None
                ):
                    raise ValueError("Invalid username or password")
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
                raise ValueError("Invalid username or password")
        else:
            from dataclasses import replace

            verified = replace(verified, mfa_required=mfa_required)
        # Resolve delivery inputs before entering the transaction-free owner.
        context_token = (
            _customer_mfa_context_token(
                db,
                username=normalized_username,
                account_id=account_id,
                subscriber_id=subscriber_id,
                subscription_id=subscription_id,
                remember=remember,
                next_url=next_url,
            )
            if mfa_required
            else None
        )
        outcome = pa.complete_password_step(
            db,
            verified,
            request=request,
            session_sink="customer_portal",
            expires_at=_portal_expiry(db, remember),
        )
        if outcome.kind == "mfa_challenge":
            response = RedirectResponse(url="/portal/auth/mfa", status_code=303)
            _set_customer_mfa_cookies(
                response,
                mfa_token=outcome.challenge_token or "",
                context_token=context_token or "",
            )
            return response
        if outcome.staged_session is None:
            raise RuntimeError("Customer session was not issued")
        session_token = _deliver_customer_session(
            db,
            staged=outcome.staged_session,
            username=normalized_username,
            account_id=account_id,
            subscriber_id=subscriber_id,
            subscription_id=subscription_id,
            remember=remember,
        )

        redirect_url = _safe_next(next_url)
        response = RedirectResponse(url=redirect_url, status_code=303)

        max_age = (
            customer_portal.get_remember_max_age(db)
            if remember
            else customer_portal.get_session_max_age(db)
        )
        response.set_cookie(
            key=customer_portal.SESSION_COOKIE_NAME,
            value=session_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=max_age,
        )

        return response

    except (RuntimeError, pa.PasswordAuthenticationError) as exc:
        if (
            isinstance(exc, pa.PasswordAuthenticationError)
            and exc.kind != "lock_timeout"
        ):
            return templates.TemplateResponse(
                "customer/auth/login.html",
                {
                    "request": request,
                    "error": exc.message,
                    "next": next_url,
                    "username": username,
                },
                status_code=401,
            )
        error_msg = "Session service unavailable. Please try again."
        return templates.TemplateResponse(
            "customer/auth/login.html",
            {
                "request": request,
                "error": error_msg,
                "next": next_url,
                "username": username,
            },
            status_code=503,
            headers={"Retry-After": "5"}
            if isinstance(exc, pa.PasswordAuthenticationError)
            and exc.kind == "lock_timeout"
            else None,
        )
    except Exception as exc:
        error_msg = "Invalid username or password"
        message = str(exc).lower()
        if isinstance(exc, customer_login_identity.CustomerLoginIdentityError):
            error_msg = exc.message
        elif "account locked" in message:
            error_msg = str(exc)
        elif "account disabled" in message:
            error_msg = str(exc)
        elif "password reset required" in message:
            error_msg = str(exc)
        elif "customer account not found" in message:
            error_msg = str(exc)
        elif "timeout" in message:
            error_msg = "Authentication service unavailable. Please try again."
        elif "not configured" in message:
            error_msg = "Authentication service not configured. Please contact support."

        return templates.TemplateResponse(
            "customer/auth/login.html",
            {
                "request": request,
                "error": error_msg,
                "next": next_url,
                "username": username,
            },
            status_code=401,
        )


def customer_mfa_page(request: Request, db: Session, error: str | None = None):
    mfa_token = request.cookies.get(_CUSTOMER_MFA_TOKEN_COOKIE)
    context_token = request.cookies.get(_CUSTOMER_MFA_CONTEXT_COOKIE)
    if not mfa_token or not context_token:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    try:
        _decode_customer_mfa_context(db, context_token)
    except Exception:
        response = RedirectResponse(url="/portal/auth/login", status_code=303)
        _clear_customer_mfa_cookies(response)
        return response
    return templates.TemplateResponse(
        request,
        "customer/auth/mfa.html",
        {"request": request, "error": error},
    )


def customer_mfa_submit(request: Request, db: Session, code: str):
    mfa_token = request.cookies.get(_CUSTOMER_MFA_TOKEN_COOKIE)
    context_token = request.cookies.get(_CUSTOMER_MFA_CONTEXT_COOKIE)
    if not mfa_token or not context_token:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    try:
        context = _decode_customer_mfa_context(db, context_token)
        staged = _verify_customer_mfa_token(
            db,
            mfa_token=mfa_token,
            context=context,
            code=code,
            request=request,
        )
        session_token = _deliver_customer_session(
            db,
            staged=staged,
            username=str(context.get("username") or ""),
            account_id=UUID(str(context["account_id"])),
            subscriber_id=UUID(str(context["subscriber_id"])),
            subscription_id=context.get("subscription_id"),
            remember=bool(context.get("remember")),
        )
        response = RedirectResponse(
            url=_safe_next(str(context.get("next") or "")),
            status_code=303,
        )
        _clear_customer_mfa_cookies(response)
        response.set_cookie(
            key=customer_portal.SESSION_COOKIE_NAME,
            value=session_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=customer_portal.get_remember_max_age(db)
            if context.get("remember")
            else customer_portal.get_session_max_age(db),
        )
        return response
    except RuntimeError:
        return templates.TemplateResponse(
            request,
            "customer/auth/mfa.html",
            {
                "request": request,
                "error": "Session service unavailable. Please try again.",
            },
            status_code=503,
        )
    except pa.PasswordAuthenticationError as exc:
        return templates.TemplateResponse(
            request,
            "customer/auth/mfa.html",
            {"request": request, "error": exc.message},
            status_code=503 if exc.kind == "lock_timeout" else 401,
            headers={"Retry-After": "5"} if exc.kind == "lock_timeout" else None,
        )
    except HTTPException as exc:
        error_msg = "Invalid verification code"
        status_code = 401
        if exc.status_code == 429 and isinstance(exc.detail, str):
            error_msg = exc.detail
            status_code = 429
        return templates.TemplateResponse(
            request,
            "customer/auth/mfa.html",
            {"request": request, "error": error_msg},
            status_code=status_code,
        )
    except Exception:
        return templates.TemplateResponse(
            request,
            "customer/auth/mfa.html",
            {"request": request, "error": "Invalid verification code"},
            status_code=401,
        )


def customer_support_info(request: Request, db: Session) -> Response:
    """Render public support contact page (no auth required)."""
    from app.services.web_system_company_info import get_company_info

    info = get_company_info(db)
    return templates.TemplateResponse(
        "customer/auth/support_info.html",
        {
            "request": request,
            "company_name": info.get("company_name") or "",
            "company_email": info.get("company_email") or "",
            "company_phone": info.get("company_phone") or "",
        },
    )


def customer_logout(request: Request):
    session_token = request.cookies.get(customer_portal.SESSION_COOKIE_NAME)
    if session_token:
        customer_portal.invalidate_customer_session(session_token)

    response = RedirectResponse(url="/portal/auth/login", status_code=303)
    response.delete_cookie(customer_portal.SESSION_COOKIE_NAME)
    _clear_customer_mfa_cookies(response)
    return response


def customer_stop_impersonation(request: Request, next_url: str | None):
    session_token = request.cookies.get(customer_portal.SESSION_COOKIE_NAME)
    if session_token:
        customer_portal.invalidate_customer_session(session_token)

    safe_url = _safe_next(next_url, fallback="/admin/customers")
    response = RedirectResponse(url=safe_url, status_code=303)
    response.delete_cookie(customer_portal.SESSION_COOKIE_NAME)
    return response


def customer_session_info(request: Request, db: Session):
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return HTMLResponse(
            content='<div class="text-red-500">Session expired</div>',
            headers={"HX-Redirect": "/portal/auth/login"},
        )

    escaped_username = html.escape(str(customer.get("username", "")))
    return HTMLResponse(
        content=f'<span class="text-green-500">Logged in as {escaped_username}</span>'
    )


def customer_refresh(request: Request, db: Session):
    session_token = request.cookies.get(customer_portal.SESSION_COOKIE_NAME)
    if not session_token:
        return Response(status_code=401)

    session = customer_portal.refresh_customer_session(session_token, db)
    if not session:
        return Response(status_code=401)

    max_age = (
        customer_portal.get_remember_max_age(db)
        if session.get("remember")
        else customer_portal.get_session_max_age(db)
    )

    expires_at = datetime.fromisoformat(str(session["expires_at"]))
    max_age = min(
        max_age, max(1, int((expires_at - datetime.now(UTC)).total_seconds()))
    )

    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        key=customer_portal.SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=max_age,
    )
    renew_csrf_cookie(response, request)
    return response
