"""Reseller portal routes."""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services import web_reseller_routes as web_reseller_routes_service
router = APIRouter(prefix="/reseller", tags=["web-reseller"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("", response_class=HTMLResponse)
def reseller_home(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_home(request, db)


@router.get("/dashboard", response_class=HTMLResponse)
def reseller_dashboard(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_dashboard(
        request, db, page, per_page
    )


@router.get("/accounts", response_class=HTMLResponse)
def reseller_accounts(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=5, le=100),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_accounts(
        request, db, page, per_page
    )


@router.post("/accounts/{account_id}/view", response_class=HTMLResponse)
def reseller_account_view(
    request: Request,
    account_id: str,
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_account_view(
        request, db, account_id
    )


@router.get("/fiber-map", response_class=HTMLResponse)
def reseller_fiber_map(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_fiber_map(request, db)
