"""Reseller portal authentication."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services import web_reseller_auth as web_reseller_auth_service
router = APIRouter(prefix="/reseller/auth", tags=["web-reseller-auth"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/login", response_class=HTMLResponse)
def reseller_login_page(request: Request, error: str | None = None):
    return web_reseller_auth_service.reseller_login_page(request, error)


@router.post("/login", response_class=HTMLResponse)
def reseller_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember: bool = Form(False),
    db: Session = Depends(get_db),
):
    return web_reseller_auth_service.reseller_login_submit(
        request, db, username, password, remember
    )


@router.get("/mfa", response_class=HTMLResponse)
def reseller_mfa_page(request: Request, error: str | None = None):
    return web_reseller_auth_service.reseller_mfa_page(request, error)


@router.post("/mfa", response_class=HTMLResponse)
def reseller_mfa_submit(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    return web_reseller_auth_service.reseller_mfa_submit(request, db, code)


@router.get("/logout")
def reseller_logout(request: Request):
    return web_reseller_auth_service.reseller_logout(request)


@router.get("/refresh")
def reseller_refresh(request: Request):
    return web_reseller_auth_service.reseller_refresh(request)
