"""Service helpers for vendor auth routes."""

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services import vendor_portal

templates = Jinja2Templates(directory="templates")


def vendor_login_page(request: Request, error: str | None = None):
    with SessionLocal() as db:
        context = vendor_portal.get_context(
            db, request.cookies.get(vendor_portal.SESSION_COOKIE_NAME)
        )
        if context:
            return RedirectResponse(url="/vendor/dashboard", status_code=303)
    return templates.TemplateResponse(
        "vendor/auth/login.html",
        {"request": request, "error": error},
    )


def vendor_login_submit(
    request: Request,
    db: Session,
    username: str,
    password: str,
    remember: bool,
):
    try:
        result = vendor_portal.login(db, username, password, request, remember)
        if result.get("mfa_required"):
            response = RedirectResponse(url="/vendor/auth/mfa", status_code=303)
            response.set_cookie(
                key="vendor_mfa_pending",
                value=result.get("mfa_token", ""),
                httponly=True,
                secure=True,
                samesite="lax",
                max_age=300,
            )
            response.set_cookie(
                key="vendor_mfa_remember",
                value="1" if remember else "0",
                httponly=True,
                secure=True,
                samesite="lax",
                max_age=300,
            )
            return response

        session_token = result.get("session_token")
        response = RedirectResponse(url="/vendor/dashboard", status_code=303)
        max_age = vendor_portal.get_remember_max_age(db) if remember else vendor_portal.get_session_max_age(db)
        response.set_cookie(
            key=vendor_portal.SESSION_COOKIE_NAME,
            value=session_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=max_age,
        )
        return response
    except Exception as exc:
        error_msg = str(exc) if str(exc) else "Invalid credentials"
        return templates.TemplateResponse(
            "vendor/auth/login.html",
            {"request": request, "error": error_msg},
            status_code=401,
        )


def vendor_mfa_page(request: Request, error: str | None = None):
    mfa_pending = request.cookies.get("vendor_mfa_pending")
    if not mfa_pending:
        return RedirectResponse(url="/vendor/auth/login", status_code=303)
    return templates.TemplateResponse(
        "vendor/auth/mfa.html",
        {"request": request, "error": error},
    )


def vendor_mfa_submit(
    request: Request,
    db: Session,
    code: str,
):
    mfa_token = request.cookies.get("vendor_mfa_pending")
    if not mfa_token:
        return RedirectResponse(url="/vendor/auth/login", status_code=303)
    try:
        remember = request.cookies.get("vendor_mfa_remember") == "1"
        result = vendor_portal.verify_mfa(db, mfa_token, code, request, remember)
        session_token = result.get("session_token")
        response = RedirectResponse(url="/vendor/dashboard", status_code=303)
        response.delete_cookie("vendor_mfa_pending")
        response.delete_cookie("vendor_mfa_remember")
        response.set_cookie(
            key=vendor_portal.SESSION_COOKIE_NAME,
            value=session_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=vendor_portal.get_remember_max_age(db) if remember else vendor_portal.get_session_max_age(db),
        )
        return response
    except Exception:
        return templates.TemplateResponse(
            "vendor/auth/mfa.html",
            {"request": request, "error": "Invalid verification code"},
            status_code=401,
        )


def vendor_logout(request: Request):
    session_token = request.cookies.get(vendor_portal.SESSION_COOKIE_NAME)
    if session_token:
        vendor_portal.invalidate_session(session_token)
    response = RedirectResponse(url="/vendor/auth/login", status_code=303)
    response.delete_cookie(vendor_portal.SESSION_COOKIE_NAME)
    response.delete_cookie("vendor_mfa_pending")
    response.delete_cookie("vendor_mfa_remember")
    return response


def vendor_refresh(request: Request):
    session_token = request.cookies.get(vendor_portal.SESSION_COOKIE_NAME)
    if not session_token:
        return Response(status_code=401)

    db = SessionLocal()
    try:
        session = vendor_portal.refresh_session(session_token, db)
        if not session:
            return Response(status_code=401)

        max_age = vendor_portal.get_remember_max_age(db) if session.get("remember") else vendor_portal.get_session_max_age(db)
    finally:
        db.close()

    response = Response(status_code=204)
    response.set_cookie(
        key=vendor_portal.SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=max_age,
    )
    return response
