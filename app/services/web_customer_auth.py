"""Service helpers for customer portal auth."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.auth import AuthProvider, UserCredential
from app.models.catalog import AccessCredential
from app.models.domain_settings import SettingDomain
from app.models.radius import RadiusUser
from app.models.subscriber import Subscriber
from app.services import customer_portal
from app.services import radius_auth
from app.services.auth_flow import verify_password
from app.services.settings_spec import resolve_value

templates = Jinja2Templates(directory="templates")

def _setting_int(db: Session, domain: SettingDomain, key: str, default: int) -> int:
    raw = resolve_value(db, domain, key)
    if raw is None:
        return default
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


def get_current_customer_from_request(request: Request, db: Session) -> Optional[dict]:
    session_token = request.cookies.get(customer_portal.SESSION_COOKIE_NAME)
    return customer_portal.get_current_customer(session_token, db)


def customer_login_page(request: Request, error: str | None = None, next_url: str | None = None):
    db = SessionLocal()
    try:
        customer = get_current_customer_from_request(request, db)
        if customer:
            return RedirectResponse(url=next_url or "/portal/dashboard", status_code=303)
    finally:
        db.close()

    return templates.TemplateResponse(
        "customer/auth/login.html",
        {"request": request, "error": error, "next": next_url},
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

        local_credential = (
            db.query(UserCredential)
            .filter(UserCredential.username == normalized_username)
            .filter(UserCredential.provider == AuthProvider.local)
            .first()
        )
        if local_credential:
            if not local_credential.is_active:
                raise ValueError("Account disabled. Please contact support.")
            now = datetime.now(timezone.utc)
            if local_credential.locked_until and local_credential.locked_until > now:
                raise ValueError("Account locked. Please try again later.")
            if not verify_password(password, local_credential.password_hash):
                local_credential.failed_login_attempts += 1
                max_attempts = _setting_int(
                    db, SettingDomain.auth, "customer_login_max_attempts", 5
                )
                lockout_minutes = _setting_int(
                    db, SettingDomain.auth, "customer_lockout_minutes", 15
                )
                if local_credential.failed_login_attempts >= max_attempts:
                    local_credential.locked_until = now + timedelta(minutes=lockout_minutes)
                db.commit()
                raise ValueError("Invalid username or password")

            if local_credential.must_change_password:
                raise ValueError("Password reset required. Please contact support.")

            local_credential.failed_login_attempts = 0
            local_credential.locked_until = None
            local_credential.last_login_at = now
            db.commit()
            authenticated_locally = True

            subscriber = db.get(Subscriber, local_credential.subscriber_id)
            if subscriber and subscriber.is_active:
                subscriber_id = subscriber.id
                account_id = subscriber.id

        if not authenticated_locally:
            radius_auth.authenticate(db=db, username=normalized_username, password=password)

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
                    account_id = credential.subscriber_id
                    subscriber_id = credential.subscriber_id
                if account_id and not subscriber_id:
                    account = db.get(Subscriber, account_id)
                    if account:
                        subscriber_id = account.id

        if not account_id or not subscriber_id:
            raise ValueError("Customer account not found. Please contact support.")

        session_token = customer_portal.create_customer_session(
            username=normalized_username,
            account_id=account_id,
            subscriber_id=subscriber_id,
            subscription_id=subscription_id,
            remember=remember,
            db=db,
        )

        redirect_url = next_url or "/portal/dashboard"
        response = RedirectResponse(url=redirect_url, status_code=303)

        max_age = customer_portal.get_remember_max_age(db) if remember else customer_portal.get_session_max_age(db)
        response.set_cookie(
            key=customer_portal.SESSION_COOKIE_NAME,
            value=session_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=max_age,
        )

        return response

    except RuntimeError:
        error_msg = "Session service unavailable. Please try again."
        return templates.TemplateResponse(
            "customer/auth/login.html",
            {"request": request, "error": error_msg, "next": next_url, "username": username},
            status_code=503,
        )
    except Exception as exc:
        error_msg = "Invalid username or password"
        message = str(exc).lower()
        if "account locked" in message:
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
            {"request": request, "error": error_msg, "next": next_url, "username": username},
            status_code=401,
        )


def customer_logout(request: Request):
    session_token = request.cookies.get(customer_portal.SESSION_COOKIE_NAME)
    if session_token:
        customer_portal.invalidate_customer_session(session_token)

    response = RedirectResponse(url="/portal/auth/login", status_code=303)
    response.delete_cookie(customer_portal.SESSION_COOKIE_NAME)
    return response


def customer_stop_impersonation(request: Request, next_url: str | None):
    session_token = request.cookies.get(customer_portal.SESSION_COOKIE_NAME)
    if session_token:
        customer_portal.invalidate_customer_session(session_token)

    response = RedirectResponse(url=next_url or "/admin/customers", status_code=303)
    response.delete_cookie(customer_portal.SESSION_COOKIE_NAME)
    return response


def customer_session_info(request: Request, db: Session):
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return HTMLResponse(
            content='<div class="text-red-500">Session expired</div>',
            headers={"HX-Redirect": "/portal/auth/login"},
        )

    return HTMLResponse(
        content=f'<span class="text-green-500">Logged in as {customer.get("username")}</span>'
    )


def customer_refresh(request: Request):
    session_token = request.cookies.get(customer_portal.SESSION_COOKIE_NAME)
    if not session_token:
        return Response(status_code=401)

    db = SessionLocal()
    try:
        session = customer_portal.refresh_customer_session(session_token, db)
        if not session:
            return Response(status_code=401)

        max_age = customer_portal.get_remember_max_age(db) if session.get("remember") else customer_portal.get_session_max_age(db)
    finally:
        db.close()

    response = Response(status_code=204)
    response.set_cookie(
        key=customer_portal.SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=max_age,
    )
    return response
