"""Authentication web routes for login, logout, MFA, and password reset."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_auth as web_auth_service
router = APIRouter(prefix="/auth", tags=["web-auth"])


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None, next: str | None = None):
    """Display the login page."""
    return web_auth_service.login_page(request, error, next)


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember: bool = Form(False),
    next: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Process login form submission."""
    return web_auth_service.login_submit(
        request, db, username, password, remember, next
    )


@router.get("/mfa", response_class=HTMLResponse)
def mfa_page(request: Request, error: str | None = None, next: str | None = None):
    """Display the MFA verification page."""
    return web_auth_service.mfa_page(request, next, error)


@router.post("/mfa", response_class=HTMLResponse)
def mfa_submit(
    request: Request,
    code: str = Form(...),
    next: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Process MFA code verification."""
    return web_auth_service.mfa_submit(request, db, code, next)


@router.get("/refresh")
def refresh(request: Request, next: str | None = None, db: Session = Depends(get_db)):
    """Refresh access token using refresh cookie."""
    return web_auth_service.refresh(request, db, next)


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request, success: bool = False):
    """Display the forgot password page."""
    return web_auth_service.forgot_password_page(request, success)


@router.post("/forgot-password", response_class=HTMLResponse)
def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    """Process forgot password form submission."""
    return web_auth_service.forgot_password_submit(request, db, email)


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str, error: str | None = None):
    """Display the password reset page."""
    return web_auth_service.reset_password_page(request, token, error)


@router.post("/reset-password", response_class=HTMLResponse)
def reset_password_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    """Process password reset form submission."""
    return web_auth_service.reset_password_submit(
        request, db, token, password, password_confirm
    )


@router.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    """Log out the current user."""
    return web_auth_service.logout(request, db)
