"""Customer portal authentication using local credentials or RADIUS."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_customer_auth as web_customer_auth_service

router = APIRouter(prefix="/portal/auth", tags=["web-customer-auth"])

def get_current_customer_from_request(request: Request, db: Session) -> dict | None:
    """Get the current customer from request cookies."""
    return web_customer_auth_service.get_current_customer_from_request(request, db)


@router.get("/login", response_class=HTMLResponse)
def customer_login_page(request: Request, error: str | None = None, next: str | None = None):
    """Display the customer login page."""
    return web_customer_auth_service.customer_login_page(request, error, next)


@router.post("/login", response_class=HTMLResponse)
def customer_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember: bool = Form(False),
    next: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Process customer login using local credentials or RADIUS authentication."""
    return web_customer_auth_service.customer_login_submit(
        request, db, username, password, remember, next
    )


@router.get("/logout")
def customer_logout(request: Request):
    """Log out the current customer."""
    return web_customer_auth_service.customer_logout(request)


@router.get("/stop-impersonation")
def customer_stop_impersonation(request: Request, next: str = "/admin/customers"):
    """Stop customer impersonation and return to admin."""
    return web_customer_auth_service.customer_stop_impersonation(request, next)


@router.get("/session", response_class=HTMLResponse)
def customer_session_info(request: Request, db: Session = Depends(get_db)):
    """Get current session info (for HTMX polling)."""
    return web_customer_auth_service.customer_session_info(request, db)


@router.get("/refresh")
def customer_refresh(request: Request):
    """Refresh the customer session cookie."""
    return web_customer_auth_service.customer_refresh(request)
