"""Customer portal web routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import customer_portal
from app.web.customer.auth import get_current_customer_from_request

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/portal", tags=["web-customer"])


@router.get("", response_class=HTMLResponse)
def portal_home(request: Request, db: Session = Depends(get_db)) -> Response:
    """Customer portal dashboard."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal", status_code=303)

    dashboard_context = customer_portal.get_dashboard_context(db, customer)
    return templates.TemplateResponse(
        "customer/dashboard/index.html",
        {
            "request": request,
            "customer": customer,
            **dashboard_context,
            "active_page": "dashboard",
        },
    )


@router.get("/dashboard", response_class=HTMLResponse)
def customer_dashboard(request: Request, db: Session = Depends(get_db)) -> Response:
    """Customer dashboard with account overview."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/dashboard", status_code=303)

    dashboard_context = customer_portal.get_dashboard_context(db, customer)

    return templates.TemplateResponse(
        "customer/dashboard/index.html",
        {
            "request": request,
            "customer": customer,
            **dashboard_context,
            "active_page": "dashboard",
        },
    )


@router.get("/billing", response_class=HTMLResponse)
def customer_billing(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """Customer billing history."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/billing", status_code=303)

    billing_data = customer_portal.get_billing_page(
        db, customer, status=status, page=page, per_page=per_page
    )

    return templates.TemplateResponse(
        "customer/billing/index.html",
        {
            "request": request,
            "customer": customer,
            **billing_data,
            "active_page": "billing",
        },
    )


@router.get("/billing/invoices", response_class=HTMLResponse)
def customer_billing_invoices_redirect(request: Request) -> RedirectResponse:
    target = "/portal/billing"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/billing/pay", response_class=HTMLResponse)
def customer_billing_pay_redirect(request: Request) -> RedirectResponse:
    target = "/portal/billing"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/billing/invoices/{invoice_id}", response_class=HTMLResponse)
def customer_invoice_detail(
    request: Request,
    invoice_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """View invoice details."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    detail = customer_portal.get_invoice_detail(db, customer, str(invoice_id))
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/billing/invoice.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "active_page": "billing",
        },
    )


@router.get("/usage", response_class=HTMLResponse)
def customer_usage(
    request: Request,
    period: str = "current",
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> Response:
    """Customer usage dashboard."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/usage", status_code=303)

    usage_data = customer_portal.get_usage_page(
        db, customer, period=period, page=page, per_page=per_page
    )

    return templates.TemplateResponse(
        "customer/usage/index.html",
        {
            "request": request,
            "customer": customer,
            **usage_data,
            "active_page": "usage",
        },
    )


@router.get("/account", response_class=HTMLResponse)
def customer_account_root_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/portal/profile", status_code=303)


@router.get("/account/{path:path}", response_class=HTMLResponse)
def customer_account_path_redirect(request: Request, path: str) -> RedirectResponse:
    target = f"/portal/{path}" if path else "/portal/profile"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/services", response_class=HTMLResponse)
def customer_services(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """Customer active services."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/services", status_code=303)

    services_data = customer_portal.get_services_page(
        db, customer, status=status, page=page, per_page=per_page
    )

    return templates.TemplateResponse(
        "customer/services/index.html",
        {
            "request": request,
            "customer": customer,
            **services_data,
            "active_page": "services",
        },
    )


@router.get("/services/{subscription_id}", response_class=HTMLResponse)
def customer_service_detail(
    request: Request,
    subscription_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Customer service detail page."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    detail = customer_portal.get_service_detail(db, customer, str(subscription_id))
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Subscription not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/services/detail.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "active_page": "services",
        },
    )


@router.get("/installations", response_class=HTMLResponse)
def customer_installations(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """Customer installation appointments."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/installations", status_code=303)

    appt_data = customer_portal.get_customer_appointments(
        db=db,
        customer=customer,
        status=status,
        page=page,
        per_page=per_page,
    )

    return templates.TemplateResponse(
        "customer/installations/index.html",
        {
            "request": request,
            "customer": customer,
            "appointments": appt_data["appointments"],
            "status": status,
            "page": page,
            "per_page": per_page,
            "total": appt_data["total"],
            "total_pages": appt_data["total_pages"],
            "active_page": "installations",
        },
    )


@router.get("/service-orders", response_class=HTMLResponse)
def customer_service_orders(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """Customer service orders."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/service-orders", status_code=303)

    orders_data = customer_portal.get_service_orders_page(
        db, customer, status=status, page=page, per_page=per_page
    )

    return templates.TemplateResponse(
        "customer/service-orders/index.html",
        {
            "request": request,
            "customer": customer,
            **orders_data,
            "active_page": "service-orders",
        },
    )


@router.get("/installations/{appointment_id}", response_class=HTMLResponse)
def customer_installation_detail(
    request: Request,
    appointment_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Customer installation appointment detail view."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    detail = customer_portal.get_installation_detail(
        db, customer, str(appointment_id)
    )
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Installation not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/installations/detail.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "active_page": "installations",
        },
    )


@router.get("/service-orders/{service_order_id}", response_class=HTMLResponse)
def customer_service_order_detail(
    request: Request,
    service_order_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Customer service order detail view."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    detail = customer_portal.get_service_order_detail(
        db, customer, str(service_order_id)
    )
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Service order not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/service-orders/detail.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "active_page": "service-orders",
        },
    )


@router.get("/profile", response_class=HTMLResponse)
def customer_profile(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Customer profile settings."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/profile", status_code=303)

    return templates.TemplateResponse(
        "customer/profile/index.html",
        {
            "request": request,
            "customer": customer,
            "active_page": "profile",
        },
    )


@router.post("/profile", response_class=HTMLResponse)
def customer_update_profile(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Update customer profile."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    # In real implementation, update customer profile
    return templates.TemplateResponse(
        "customer/profile/index.html",
        {
            "request": request,
            "customer": customer,
            "success": "Profile updated successfully",
            "active_page": "profile",
        },
    )


# =============================================================================
# Plan Change Self-Service
# =============================================================================

@router.get("/services/{subscription_id}/change", response_class=HTMLResponse)
def customer_change_plan(
    request: Request,
    subscription_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Show available plans for changing subscription."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    page_data = customer_portal.get_change_plan_page(
        db, customer, str(subscription_id)
    )
    if not page_data:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Subscription not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/services/change_plan.html",
        {
            "request": request,
            "customer": customer,
            **page_data,
            "active_page": "services",
        },
    )


@router.post("/services/{subscription_id}/change", response_class=HTMLResponse)
def customer_submit_change_plan(
    request: Request,
    subscription_id: UUID,
    offer_id: str = Form(...),
    effective_date: str = Form(...),
    notes: str = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Submit a plan change request."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    try:
        customer_portal.submit_change_plan(
            db=db,
            customer=customer,
            subscription_id=str(subscription_id),
            offer_id=offer_id,
            effective_date=effective_date,
            notes=notes,
        )
        return RedirectResponse(
            url="/portal/change-requests?submitted=true",
            status_code=303,
        )
    except Exception as exc:
        error_ctx = customer_portal.get_change_plan_error_context(
            db, str(subscription_id)
        )
        return templates.TemplateResponse(
            "customer/services/change_plan.html",
            {
                "request": request,
                "customer": customer,
                **error_ctx,
                "error": str(exc),
                "active_page": "services",
            },
            status_code=400,
        )


@router.get("/change-requests", response_class=HTMLResponse)
def customer_change_requests(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """List pending plan change requests."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/change-requests", status_code=303)

    change_data = customer_portal.get_change_requests_page(
        db, customer, status=status, page=page, per_page=per_page
    )

    return templates.TemplateResponse(
        "customer/services/change_requests.html",
        {
            "request": request,
            "customer": customer,
            **change_data,
            "active_page": "services",
        },
    )


# =============================================================================
# Payment Arrangements Self-Service
# =============================================================================

@router.get("/billing/arrangements", response_class=HTMLResponse)
def customer_payment_arrangements(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """List payment arrangements for the customer."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/billing/arrangements", status_code=303)

    arrangements_data = customer_portal.get_payment_arrangements_page(
        db, customer, status=status, page=page, per_page=per_page
    )

    return templates.TemplateResponse(
        "customer/billing/arrangements.html",
        {
            "request": request,
            "customer": customer,
            **arrangements_data,
            "active_page": "billing",
        },
    )


@router.get("/billing/arrangements/new", response_class=HTMLResponse)
def customer_new_payment_arrangement(
    request: Request,
    invoice_id: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Show form to request a new payment arrangement."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    page_data = customer_portal.get_new_arrangement_page(
        db, customer, invoice_id=invoice_id
    )

    return templates.TemplateResponse(
        "customer/billing/arrangement_form.html",
        {
            "request": request,
            "customer": customer,
            **page_data,
            "active_page": "billing",
        },
    )


@router.post("/billing/arrangements", response_class=HTMLResponse)
def customer_submit_payment_arrangement(
    request: Request,
    total_amount: str = Form(...),
    installments: int = Form(...),
    frequency: str = Form(...),
    start_date: str = Form(...),
    invoice_id: str = Form(None),
    notes: str = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Submit a payment arrangement request."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    try:
        customer_portal.submit_payment_arrangement(
            db=db,
            customer=customer,
            total_amount=total_amount,
            installments=installments,
            frequency=frequency,
            start_date=start_date,
            invoice_id=invoice_id,
            notes=notes,
        )
        return RedirectResponse(
            url="/portal/billing/arrangements?submitted=true",
            status_code=303,
        )
    except Exception as exc:
        account_id = customer.get("account_id")
        account_id_str = str(account_id) if account_id else None
        error_ctx = customer_portal.get_arrangement_error_context(
            db, account_id_str
        )
        return templates.TemplateResponse(
            "customer/billing/arrangement_form.html",
            {
                "request": request,
                "customer": customer,
                **error_ctx,
                "error": str(exc),
                "active_page": "billing",
            },
            status_code=400,
        )


@router.get("/billing/arrangements/{arrangement_id}", response_class=HTMLResponse)
def customer_payment_arrangement_detail(
    request: Request,
    arrangement_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """View payment arrangement details."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    detail = customer_portal.get_payment_arrangement_detail(
        db, customer, str(arrangement_id)
    )
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Payment arrangement not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/billing/arrangement_detail.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "active_page": "billing",
        },
    )
