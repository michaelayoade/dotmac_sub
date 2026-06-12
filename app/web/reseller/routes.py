"""Reseller portal routes."""

from fastapi import APIRouter, Body, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_reseller_billing as web_reseller_billing_service
from app.services import web_reseller_routes as web_reseller_routes_service


def _reseller_auth_guard(request: Request, db: Session = Depends(get_db)):
    # Thin wrapper so the guard resolves at request time, avoiding the
    # import cycle that referencing the service attribute at module load
    # (in dependencies=) would trigger via the shared branding/app.web chain.
    return web_reseller_routes_service.require_reseller_context(request, db)


router = APIRouter(
    prefix="/reseller",
    tags=["web-reseller"],
    dependencies=[Depends(_reseller_auth_guard)],
)


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
    return web_reseller_routes_service.reseller_dashboard(request, db, page, per_page)


@router.get("/vas", response_class=HTMLResponse)
def reseller_vas(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_vas_page(request, db)


@router.post("/vas/topup/intent")
def reseller_vas_topup_intent(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_vas_topup_intent(
        request, db, payload.get("amount")
    )


@router.get("/vas/topup/verify", response_class=HTMLResponse)
def reseller_vas_topup_verify(
    request: Request, reference: str, db: Session = Depends(get_db)
):
    return web_reseller_routes_service.reseller_vas_topup_verify(request, db, reference)


@router.post("/vas/sell")
def reseller_vas_sell(
    request: Request,
    service_id: str = Form(...),
    identifier: str = Form(...),
    variation_code: str = Form(""),
    amount: str = Form(""),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_vas_sell(
        request,
        db,
        service_id=service_id,
        identifier=identifier,
        variation_code=variation_code,
        amount=amount,
    )


@router.get("/service-requests", response_class=HTMLResponse)
def reseller_service_requests(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_service_requests_page(request, db)


@router.post("/service-requests", response_class=HTMLResponse)
def reseller_service_request_create(
    request: Request,
    contact_name: str = Form(""),
    contact_phone: str = Form(""),
    contact_email: str = Form(""),
    address: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_service_request_create(
        request,
        db,
        contact_name=contact_name,
        contact_phone=contact_phone,
        contact_email=contact_email,
        address=address,
        latitude=latitude,
        longitude=longitude,
        notes=notes,
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
    return web_reseller_routes_service.reseller_account_detail(request, db, account_id)


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
    return web_reseller_routes_service.reseller_account_view(request, db, account_id)


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
        request,
        db,
        contact_email=contact_email or None,
        contact_phone=contact_phone or None,
        notes=notes or None,
    )


@router.post("/profile/mfa/setup", response_class=HTMLResponse)
def reseller_mfa_setup(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_mfa_setup(request, db)


@router.get("/profile/mfa/setup", response_class=HTMLResponse)
def reseller_mfa_setup_page(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_mfa_setup(request, db)


@router.post("/profile/mfa/confirm", response_class=HTMLResponse)
def reseller_mfa_confirm(
    request: Request,
    method_id: str = Form(...),
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_mfa_confirm(
        request, db, method_id, code
    )


@router.get("/accounts/{account_id}/tickets", response_class=HTMLResponse)
def reseller_account_tickets(
    request: Request,
    account_id: str,
    db: Session = Depends(get_db),
):
    return web_reseller_routes_service.reseller_account_tickets(request, db, account_id)


@router.get("/fiber-map", response_class=HTMLResponse)
def reseller_fiber_map(request: Request, db: Session = Depends(get_db)):
    return web_reseller_routes_service.reseller_fiber_map(request, db)


@router.get("/billing", response_class=HTMLResponse)
def reseller_billing(request: Request, db: Session = Depends(get_db)):
    return web_reseller_billing_service.billing_overview(request, db)


@router.post("/billing/pay/intent", response_class=HTMLResponse)
def reseller_billing_pay_intent(
    request: Request,
    amount: str = Form(...),
    db: Session = Depends(get_db),
):
    return web_reseller_billing_service.billing_pay_intent(request, db, amount)


@router.get("/billing/pay/verify", response_class=HTMLResponse)
def reseller_billing_pay_verify(
    request: Request,
    reference: str = Query(...),
    provider: str | None = Query(None),
    db: Session = Depends(get_db),
):
    return web_reseller_billing_service.billing_pay_verify(
        request, db, reference, provider=provider
    )
