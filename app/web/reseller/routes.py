"""Reseller portal routes."""

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_reseller_routes as web_reseller_routes_service

router = APIRouter(prefix="/reseller", tags=["web-reseller"])


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
    search: str = Query(""),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_accounts(
        request, db, page, per_page, search=search or None
    )


@router.get("/accounts/{account_id}", response_class=HTMLResponse)
def reseller_account_detail(
    request: Request,
    account_id: str,
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_account_detail(
        request, db, account_id
    )


@router.get("/accounts/{account_id}/invoices", response_class=HTMLResponse)
def reseller_account_invoices(
    request: Request,
    account_id: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=5, le=100),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_account_invoices(
        request, db, account_id, page, per_page
    )


@router.get("/accounts/{account_id}/invoices/{invoice_id}", response_class=HTMLResponse)
def reseller_invoice_detail(
    request: Request,
    account_id: str,
    invoice_id: str,
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_invoice_detail(
        request, db, account_id, invoice_id
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


@router.get("/reports/revenue", response_class=HTMLResponse)
def reseller_revenue_report(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_revenue_report(request, db)


@router.get("/profile", response_class=HTMLResponse)
def reseller_profile(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_profile(request, db)


@router.post("/profile", response_class=HTMLResponse)
def reseller_profile_update(
    request: Request,
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_profile_update(
        request, db,
        contact_email=contact_email or None,
        contact_phone=contact_phone or None,
        notes=notes or None,
    )


@router.get("/accounts/{account_id}/tickets", response_class=HTMLResponse)
def reseller_account_tickets(
    request: Request,
    account_id: str,
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_account_tickets(
        request, db, account_id
    )


@router.get("/fiber-map", response_class=HTMLResponse)
def reseller_fiber_map(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_fiber_map(request, db)
