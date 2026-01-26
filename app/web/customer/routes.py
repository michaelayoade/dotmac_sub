"""Customer portal web routes."""

from fastapi import APIRouter, Cookie, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import date
from typing import Optional
from uuid import UUID

from app.db import SessionLocal
from app.models.subscriber import Subscriber
from app.services import (
    customer_portal,
    subscriber as subscriber_service,
    billing as billing_service,
    catalog as catalog_service,
    provisioning as provisioning_service,
    projects as projects_service,
    tickets as tickets_service,
    usage as usage_service,
    workforce as workforce_service,
)
from app.web.customer.auth import get_current_customer_from_request

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/portal", tags=["web-customer"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _resolve_customer_account(customer: dict, db: Session) -> tuple[str | None, str | None]:
    """Resolve account and subscription IDs from customer session."""
    return customer_portal.resolve_customer_account(customer, db)


def _resolve_next_billing_date(db: Session, subscription) -> date | None:
    if not subscription:
        return None
    if getattr(subscription, "next_billing_at", None):
        return subscription.next_billing_at.date()
    start_at = getattr(subscription, "start_at", None) or getattr(subscription, "created_at", None)
    offer_id = getattr(subscription, "offer_id", None)
    if not start_at or not offer_id:
        return None
    from app.services.catalog.subscriptions import _compute_next_billing_at, _resolve_billing_cycle

    offer_version_id = getattr(subscription, "offer_version_id", None)
    cycle = _resolve_billing_cycle(
        db,
        str(offer_id),
        str(offer_version_id) if offer_version_id else None,
    )
    next_bill = _compute_next_billing_at(start_at, cycle)
    for _ in range(240):
        if next_bill.date() >= date.today():
            break
        next_bill = _compute_next_billing_at(next_bill, cycle)
    return next_bill.date()


@router.get("", response_class=HTMLResponse)
def portal_home(request: Request, db: Session = Depends(get_db)):
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
def customer_dashboard(request: Request, db: Session = Depends(get_db)):
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
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
):
    """Customer billing history."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/billing", status_code=303)

    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    if status == "pending":
        status = "issued"

    if not account_id_str:
        return templates.TemplateResponse(
            "customer/billing/index.html",
            {
                "request": request,
                "customer": customer,
                "invoices": [],
                "status": status,
                "page": page,
                "per_page": per_page,
                "total": 0,
                "total_pages": 1,
                "active_page": "billing",
            },
        )

    # Get customer's invoices
    invoices = billing_service.invoices.list(
        db=db,
        account_id=account_id_str,
        status=status if status else None,
        is_active=None,
        order_by="issued_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    all_invoices = billing_service.invoices.list(
        db=db,
        account_id=account_id_str,
        status=status if status else None,
        is_active=None,
        order_by="issued_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_invoices)
    total_pages = (total + per_page - 1) // per_page if total else 1

    return templates.TemplateResponse(
        "customer/billing/index.html",
        {
            "request": request,
            "customer": customer,
            "invoices": invoices,
            "status": status,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "active_page": "billing",
        },
    )


@router.get("/billing/invoices", response_class=HTMLResponse)
def customer_billing_invoices_redirect(request: Request):
    target = "/portal/billing"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/billing/pay", response_class=HTMLResponse)
def customer_billing_pay_redirect(request: Request):
    target = "/portal/billing"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/billing/invoices/{invoice_id}", response_class=HTMLResponse)
def customer_invoice_detail(
    request: Request,
    invoice_id: UUID,
    db: Session = Depends(get_db),
):
    """View invoice details."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    allowed_account_ids = customer_portal.get_allowed_account_ids(customer, db)

    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice or (
        allowed_account_ids
        and str(getattr(invoice, "account_id", "")) not in allowed_account_ids
    ):
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )

    billing_contact = customer_portal.get_invoice_billing_contact(db, invoice, customer)

    return templates.TemplateResponse(
        "customer/billing/invoice.html",
        {
            "request": request,
            "customer": customer,
            "invoice": invoice,
            "billing_name": billing_contact["billing_name"],
            "billing_email": billing_contact["billing_email"],
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
):
    """Customer usage dashboard."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/usage", status_code=303)

    subscription_id = customer.get("subscription_id")
    subscription_id_str = str(subscription_id) if subscription_id else None

    if not subscription_id_str:
        return templates.TemplateResponse(
            "customer/usage/index.html",
            {
                "request": request,
                "customer": customer,
                "usage_records": [],
                "period": period,
                "page": page,
                "per_page": per_page,
                "total": 0,
                "total_pages": 1,
                "active_page": "usage",
            },
        )

    # Get usage records
    usage_records = usage_service.usage_records.list(
        db=db,
        subscription_id=subscription_id_str,
        quota_bucket_id=None,
        order_by="recorded_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    all_usage_records = usage_service.usage_records.list(
        db=db,
        subscription_id=subscription_id_str,
        quota_bucket_id=None,
        order_by="recorded_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_usage_records)
    total_pages = (total + per_page - 1) // per_page if total else 1

    return templates.TemplateResponse(
        "customer/usage/index.html",
        {
            "request": request,
            "customer": customer,
            "usage_records": usage_records,
            "period": period,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "active_page": "usage",
        },
    )


@router.get("/support", response_class=HTMLResponse)
def customer_support(
    request: Request,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
):
    """Customer support tickets."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/support", status_code=303)

    status_param = status
    if status == "in_progress":
        status = "on_hold"

    ticket_data = customer_portal.get_customer_tickets(
        db=db,
        customer=customer,
        status=status,
        priority=priority,
        page=page,
        per_page=per_page,
    )

    return templates.TemplateResponse(
        "customer/support/index.html",
        {
            "request": request,
            "customer": customer,
            "tickets": ticket_data["tickets"],
            "status": status_param,
            "priority": priority,
            "page": page,
            "per_page": per_page,
            "total": ticket_data["total"],
            "total_pages": ticket_data["total_pages"],
            "active_page": "support",
        },
    )


@router.get("/support/tickets", response_class=HTMLResponse)
def customer_support_tickets_redirect(request: Request):
    return RedirectResponse(url="/portal/support", status_code=303)


@router.get("/support/tickets/create", response_class=HTMLResponse)
def customer_support_tickets_create_redirect(request: Request):
    return RedirectResponse(url="/portal/support/new", status_code=303)


@router.get("/account", response_class=HTMLResponse)
def customer_account_root_redirect(request: Request):
    return RedirectResponse(url="/portal/profile", status_code=303)


@router.get("/account/{path:path}", response_class=HTMLResponse)
def customer_account_path_redirect(request: Request, path: str):
    target = f"/portal/{path}" if path else "/portal/profile"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/support/new", response_class=HTMLResponse)
def customer_new_ticket(request: Request, db: Session = Depends(get_db)):
    """Create new support ticket form."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/support/new", status_code=303)

    return templates.TemplateResponse(
        "customer/support/new.html",
        {
            "request": request,
            "customer": customer,
            "active_page": "support",
        },
    )


@router.post("/support/new", response_class=HTMLResponse)
def customer_create_ticket(
    request: Request,
    subject: str = Form(...),
    description: str = Form(...),
    priority: str = Form("normal"),
    attachments: UploadFile | list[UploadFile] | None = File(None),
    db: Session = Depends(get_db),
):
    """Create a new support ticket."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    prepared_attachments: list[dict] = []
    try:
        from app.schemas.tickets import TicketCreate
        from app.services import ticket_attachments as ticket_attachment_service

        account_id_str, subscription_id_str = _resolve_customer_account(customer, db)
        subscriber = None
        subscriber_id = customer.get("subscriber_id")
        if subscriber_id:
            subscriber = db.get(Subscriber, subscriber_id)

        prepared_attachments = ticket_attachment_service.prepare_ticket_attachments(attachments)
        saved_attachments = ticket_attachment_service.save_ticket_attachments(prepared_attachments)

        metadata = None
        if saved_attachments:
            metadata = {"attachments": saved_attachments}

        # In real implementation, set account_id from customer session
        data = TicketCreate(
            title=subject,
            description=description,
            priority=priority,
            channel="web",
            account_id=account_id_str,
            subscription_id=subscription_id_str,
            created_by_person_id=subscriber.person_id if subscriber and subscriber.person_id else None,
            metadata_=metadata,
        )
        ticket = tickets_service.tickets.create(db=db, payload=data)
        return RedirectResponse(
            url=f"/portal/support/{ticket.id}",
            status_code=303,
        )
    except Exception as e:
        from app.services import ticket_attachments as ticket_attachment_service
        ticket_attachment_service.delete_ticket_attachments(prepared_attachments)
        return templates.TemplateResponse(
            "customer/support/new.html",
            {
                "request": request,
                "customer": customer,
                "error": str(e),
                "active_page": "support",
            },
            status_code=400,
        )


@router.get("/support/{ticket_id}", response_class=HTMLResponse)
def customer_ticket_detail(
    request: Request,
    ticket_id: UUID,
    db: Session = Depends(get_db),
):
    """View ticket details."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    ticket = tickets_service.tickets.get(db=db, ticket_id=ticket_id)
    account_id_str, subscription_id_str = _resolve_customer_account(customer, db)
    subscriber = None
    subscriber_id = customer.get("subscriber_id")
    if subscriber_id:
        subscriber = db.get(Subscriber, subscriber_id)
    allowed_account_ids = []
    if subscriber:
        allowed_account_ids = [str(account.id) for account in subscriber.accounts or []]
    if account_id_str and account_id_str not in allowed_account_ids:
        allowed_account_ids.append(account_id_str)
    matches_account = (
        allowed_account_ids
        and str(getattr(ticket, "account_id", "")) in allowed_account_ids
    )
    matches_subscription = subscription_id_str and str(getattr(ticket, "subscription_id", "")) == subscription_id_str
    matches_person = (
        subscriber
        and subscriber.person_id
        and str(getattr(ticket, "created_by_person_id", "")) == str(subscriber.person_id)
    )
    if not ticket or not (matches_account or matches_subscription or matches_person):
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Ticket not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/support/detail.html",
        {
            "request": request,
            "customer": customer,
            "ticket": ticket,
            "active_page": "support",
        },
    )


@router.get("/services", response_class=HTMLResponse)
def customer_services(
    request: Request,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
):
    """Customer active services."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/services", status_code=303)

    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None
    status_param = status

    if not account_id_str:
        return templates.TemplateResponse(
            "customer/services/index.html",
            {
                "request": request,
                "customer": customer,
                "services": [],
                "status": status_param,
                "page": page,
                "per_page": per_page,
                "total": 0,
                "total_pages": 1,
                "active_page": "services",
            },
        )

    services = catalog_service.subscriptions.list(
        db=db,
        account_id=account_id_str,
        offer_id=None,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    all_services = catalog_service.subscriptions.list(
        db=db,
        account_id=account_id_str,
        offer_id=None,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_services)
    total_pages = (total + per_page - 1) // per_page if total else 1

    return templates.TemplateResponse(
        "customer/services/index.html",
        {
            "request": request,
            "customer": customer,
            "services": services,
            "status": status_param,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "active_page": "services",
        },
    )


@router.get("/services/{subscription_id}", response_class=HTMLResponse)
def customer_service_detail(
    request: Request,
    subscription_id: UUID,
    db: Session = Depends(get_db),
):
    """Customer service detail page."""
    from app.models.catalog import CatalogOffer

    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    subscription = catalog_service.subscriptions.get(db=db, subscription_id=str(subscription_id))
    if not subscription:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Subscription not found"},
            status_code=404,
        )

    account_id = customer.get("account_id")
    if account_id and str(subscription.account_id) != str(account_id):
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Subscription not found"},
            status_code=404,
        )

    current_offer = None
    if subscription.offer_id:
        current_offer = db.get(CatalogOffer, subscription.offer_id)

    next_billing_date = _resolve_next_billing_date(db, subscription)

    return templates.TemplateResponse(
        "customer/services/detail.html",
        {
            "request": request,
            "customer": customer,
            "subscription": subscription,
            "current_offer": current_offer,
            "next_billing_date": next_billing_date,
            "active_page": "services",
        },
    )

@router.get("/installations", response_class=HTMLResponse)
def customer_installations(
    request: Request,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
):
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
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
):
    """Customer service orders."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/service-orders", status_code=303)

    account_id = customer.get("account_id")
    subscription_id = customer.get("subscription_id")
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    if not account_id_str and not subscription_id_str:
        return templates.TemplateResponse(
        "customer/service-orders/index.html",
        {
            "request": request,
            "customer": customer,
            "service_orders": [],
            "status": status,
            "page": page,
            "per_page": per_page,
            "total": 0,
            "total_pages": 1,
            "active_page": "service-orders",
        },
    )

    service_orders = provisioning_service.service_orders.list(
        db=db,
        account_id=account_id_str,
        subscription_id=subscription_id_str,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    all_service_orders = provisioning_service.service_orders.list(
        db=db,
        account_id=account_id_str,
        subscription_id=subscription_id_str,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_service_orders)
    total_pages = (total + per_page - 1) // per_page if total else 1

    return templates.TemplateResponse(
        "customer/service-orders/index.html",
        {
            "request": request,
            "customer": customer,
            "service_orders": service_orders,
            "status": status,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "active_page": "service-orders",
        },
    )


@router.get("/work-orders", response_class=HTMLResponse)
def customer_work_orders(
    request: Request,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
):
    """Customer field service work orders."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/work-orders", status_code=303)

    account_id = customer.get("account_id")
    subscription_id = customer.get("subscription_id")
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    if not account_id_str and not subscription_id_str:
        return templates.TemplateResponse(
            "customer/work-orders/index.html",
            {
                "request": request,
                "customer": customer,
                "work_orders": [],
                "status": status,
                "priority": priority,
                "page": page,
                "per_page": per_page,
                "total": 0,
                "total_pages": 1,
                "active_page": "work-orders",
            },
        )

    work_orders = workforce_service.work_orders.list(
        db=db,
        account_id=account_id_str,
        subscription_id=subscription_id_str,
        service_order_id=None,
        ticket_id=None,
        project_id=None,
        assigned_to_person_id=None,
        status=status if status else None,
        priority=priority if priority else None,
        work_type=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    all_work_orders = workforce_service.work_orders.list(
        db=db,
        account_id=account_id_str,
        subscription_id=subscription_id_str,
        service_order_id=None,
        ticket_id=None,
        project_id=None,
        assigned_to_person_id=None,
        status=status if status else None,
        priority=priority if priority else None,
        work_type=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_work_orders)
    total_pages = (total + per_page - 1) // per_page if total else 1

    return templates.TemplateResponse(
        "customer/work-orders/index.html",
        {
            "request": request,
            "customer": customer,
            "work_orders": work_orders,
            "status": status,
            "priority": priority,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "active_page": "work-orders",
        },
    )


@router.get("/projects", response_class=HTMLResponse)
def customer_projects(
    request: Request,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
):
    """Customer project updates."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/projects", status_code=303)

    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    if not account_id_str:
        return templates.TemplateResponse(
            "customer/projects/index.html",
            {
                "request": request,
                "customer": customer,
                "projects": [],
                "status": status,
                "priority": priority,
                "page": page,
                "per_page": per_page,
                "total": 0,
                "total_pages": 1,
                "active_page": "projects",
            },
        )

    projects = projects_service.projects.list(
        db=db,
        account_id=account_id_str,
        status=status if status else None,
        priority=priority if priority else None,
        owner_person_id=None,
        manager_person_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    all_projects = projects_service.projects.list(
        db=db,
        account_id=account_id_str,
        status=status if status else None,
        priority=priority if priority else None,
        owner_person_id=None,
        manager_person_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_projects)
    total_pages = (total + per_page - 1) // per_page if total else 1

    return templates.TemplateResponse(
        "customer/projects/index.html",
        {
            "request": request,
            "customer": customer,
            "projects": projects,
            "status": status,
            "priority": priority,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "active_page": "projects",
        },
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def customer_project_detail(
    request: Request,
    project_id: UUID,
    db: Session = Depends(get_db),
):
    """Customer project detail view."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    project = projects_service.projects.get(db=db, project_id=str(project_id))
    if not project or (account_id_str and str(getattr(project, "account_id", "")) != account_id_str):
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Project not found"},
            status_code=404,
        )

    tasks = projects_service.project_tasks.list(
        db=db,
        project_id=str(project_id),
        status=None,
        priority=None,
        assigned_to_person_id=None,
        parent_task_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )

    return templates.TemplateResponse(
        "customer/projects/detail.html",
        {
            "request": request,
            "customer": customer,
            "project": project,
            "tasks": tasks,
            "active_page": "projects",
        },
    )


@router.get("/work-orders/{work_order_id}", response_class=HTMLResponse)
def customer_work_order_detail(
    request: Request,
    work_order_id: UUID,
    db: Session = Depends(get_db),
):
    """Customer work order detail view."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    account_id = customer.get("account_id")
    subscription_id = customer.get("subscription_id")
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    work_order = workforce_service.work_orders.get(db=db, work_order_id=str(work_order_id))
    if not work_order or (
        (account_id_str and str(getattr(work_order, "account_id", "")) != account_id_str)
        and (subscription_id_str and str(getattr(work_order, "subscription_id", "")) != subscription_id_str)
    ):
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Work order not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/work-orders/detail.html",
        {
            "request": request,
            "customer": customer,
            "work_order": work_order,
            "active_page": "work-orders",
        },
    )


@router.get("/installations/{appointment_id}", response_class=HTMLResponse)
def customer_installation_detail(
    request: Request,
    appointment_id: UUID,
    db: Session = Depends(get_db),
):
    """Customer installation appointment detail view."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    account_id = customer.get("account_id")
    subscription_id = customer.get("subscription_id")
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    appointment = provisioning_service.install_appointments.get(
        db=db, appointment_id=str(appointment_id)
    )
    service_order = provisioning_service.service_orders.get(
        db=db, order_id=str(appointment.service_order_id)
    )
    if not appointment or (
        (account_id_str and str(getattr(service_order, "account_id", "")) != account_id_str)
        and (subscription_id_str and str(getattr(service_order, "subscription_id", "")) != subscription_id_str)
    ):
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
            "appointment": appointment,
            "service_order": service_order,
            "active_page": "installations",
        },
    )


@router.get("/service-orders/{service_order_id}", response_class=HTMLResponse)
def customer_service_order_detail(
    request: Request,
    service_order_id: UUID,
    db: Session = Depends(get_db),
):
    """Customer service order detail view."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    account_id = customer.get("account_id")
    subscription_id = customer.get("subscription_id")
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    service_order = provisioning_service.service_orders.get(
        db=db, order_id=str(service_order_id)
    )
    if not service_order or (
        (account_id_str and str(getattr(service_order, "account_id", "")) != account_id_str)
        and (subscription_id_str and str(getattr(service_order, "subscription_id", "")) != subscription_id_str)
    ):
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Service order not found"},
            status_code=404,
        )

    appointments = provisioning_service.install_appointments.list(
        db=db,
        service_order_id=str(service_order_id),
        status=None,
        order_by="scheduled_start",
        order_dir="desc",
        limit=50,
        offset=0,
    )
    provisioning_tasks = provisioning_service.provisioning_tasks.list(
        db=db,
        service_order_id=str(service_order_id),
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )

    return templates.TemplateResponse(
        "customer/service-orders/detail.html",
        {
            "request": request,
            "customer": customer,
            "service_order": service_order,
            "appointments": appointments,
            "provisioning_tasks": provisioning_tasks,
            "active_page": "service-orders",
        },
    )


@router.get("/profile", response_class=HTMLResponse)
def customer_profile(
    request: Request,
    db: Session = Depends(get_db),
):
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
):
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
# Quote Self-Service
# =============================================================================

@router.get("/quotes", response_class=HTMLResponse)
def customer_quotes(
    request: Request,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
):
    """List quotes for the customer."""
    from app.services.crm import sales as crm_sales_service
    from app.models.crm.enums import QuoteStatus

    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/quotes", status_code=303)

    subscriber_id = customer.get("subscriber_id")
    if not subscriber_id:
        return templates.TemplateResponse(
            "customer/quotes/index.html",
            {
                "request": request,
                "customer": customer,
                "quotes": [],
                "status": status,
                "page": page,
                "per_page": per_page,
                "total": 0,
                "total_pages": 1,
                "active_page": "quotes",
            },
        )

    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber or not subscriber.person_id:
        return templates.TemplateResponse(
            "customer/quotes/index.html",
            {
                "request": request,
                "customer": customer,
                "quotes": [],
                "status": status,
                "page": page,
                "per_page": per_page,
                "total": 0,
                "total_pages": 1,
                "active_page": "quotes",
            },
        )

    quotes = crm_sales_service.quotes.list(
        db=db,
        person_id=str(subscriber.person_id),
        lead_id=None,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    all_quotes = crm_sales_service.quotes.list(
        db=db,
        person_id=str(subscriber.person_id),
        lead_id=None,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_quotes)
    total_pages = (total + per_page - 1) // per_page if total else 1

    return templates.TemplateResponse(
        "customer/quotes/index.html",
        {
            "request": request,
            "customer": customer,
            "quotes": quotes,
            "status": status,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "active_page": "quotes",
        },
    )


@router.get("/quotes/{quote_id}", response_class=HTMLResponse)
def customer_quote_detail(
    request: Request,
    quote_id: UUID,
    db: Session = Depends(get_db),
):
    """View quote details."""
    from app.services.crm import sales as crm_sales_service
    from app.models.crm.enums import QuoteStatus

    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    subscriber_id = customer.get("subscriber_id")
    subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None

    quote = crm_sales_service.quotes.get(db=db, quote_id=str(quote_id))
    if not quote:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Quote not found"},
            status_code=404,
        )

    # Verify quote belongs to this customer
    if subscriber and subscriber.person_id:
        if str(quote.person_id) != str(subscriber.person_id):
            return templates.TemplateResponse(
                "customer/errors/404.html",
                {"request": request, "message": "Quote not found"},
                status_code=404,
            )

    return templates.TemplateResponse(
        "customer/quotes/detail.html",
        {
            "request": request,
            "customer": customer,
            "quote": quote,
            "active_page": "quotes",
        },
    )


@router.post("/quotes/{quote_id}/accept", response_class=HTMLResponse)
def customer_accept_quote(
    request: Request,
    quote_id: UUID,
    db: Session = Depends(get_db),
):
    """Accept a quote."""
    from app.services.crm import sales as crm_sales_service
    from app.models.crm.enums import QuoteStatus

    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    try:
        crm_sales_service.quotes.accept(db=db, quote_id=str(quote_id))
        return RedirectResponse(
            url=f"/portal/quotes/{quote_id}?accepted=true",
            status_code=303,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "customer/quotes/detail.html",
            {
                "request": request,
                "customer": customer,
                "quote": crm_sales_service.quotes.get(db=db, quote_id=str(quote_id)),
                "error": str(exc),
                "active_page": "quotes",
            },
            status_code=400,
        )


@router.post("/quotes/{quote_id}/reject", response_class=HTMLResponse)
def customer_reject_quote(
    request: Request,
    quote_id: UUID,
    reason: str = Form(None),
    db: Session = Depends(get_db),
):
    """Reject a quote."""
    from app.services.crm import sales as crm_sales_service

    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    try:
        crm_sales_service.quotes.reject(db=db, quote_id=str(quote_id), reason=reason)
        return RedirectResponse(
            url="/portal/quotes?rejected=true",
            status_code=303,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "customer/quotes/detail.html",
            {
                "request": request,
                "customer": customer,
                "quote": crm_sales_service.quotes.get(db=db, quote_id=str(quote_id)),
                "error": str(exc),
                "active_page": "quotes",
            },
            status_code=400,
        )


# =============================================================================
# Plan Change Self-Service
# =============================================================================

@router.get("/services/{subscription_id}/change", response_class=HTMLResponse)
def customer_change_plan(
    request: Request,
    subscription_id: UUID,
    db: Session = Depends(get_db),
):
    """Show available plans for changing subscription."""
    from app.models.catalog import CatalogOffer

    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    # Get the subscription
    subscription = catalog_service.subscriptions.get(db=db, subscription_id=str(subscription_id))
    if not subscription:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Subscription not found"},
            status_code=404,
        )

    # Verify subscription belongs to customer
    account_id = customer.get("account_id")
    if account_id and str(subscription.account_id) != str(account_id):
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Subscription not found"},
            status_code=404,
        )

    # Get available offers from service
    available_offers = customer_portal.get_available_portal_offers(db)

    # Get current offer
    current_offer = None
    if subscription.offer_id:
        current_offer = db.get(CatalogOffer, subscription.offer_id)
    next_billing_date = _resolve_next_billing_date(db, subscription)

    return templates.TemplateResponse(
        "customer/services/change_plan.html",
        {
            "request": request,
            "customer": customer,
            "subscription": subscription,
            "current_offer": current_offer,
            "available_offers": available_offers,
            "next_billing_date": next_billing_date,
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
):
    """Submit a plan change request."""
    from app.models.catalog import CatalogOffer
    from app.services import subscription_changes as change_service
    from datetime import date, datetime

    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    # Get subscriber for person_id
    subscriber_id = customer.get("subscriber_id")
    subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None
    person_id = str(subscriber.person_id) if subscriber and subscriber.person_id else None

    try:
        eff_date = datetime.strptime(effective_date, "%Y-%m-%d").date()
        if eff_date < date.today():
            raise ValueError("Effective date must be today or later.")

        change_service.subscription_change_requests.create(
            db=db,
            subscription_id=str(subscription_id),
            new_offer_id=offer_id,
            effective_date=eff_date,
            requested_by_person_id=person_id,
            notes=notes,
        )
        return RedirectResponse(
            url="/portal/change-requests?submitted=true",
            status_code=303,
        )
    except Exception as exc:
        subscription = catalog_service.subscriptions.get(db=db, subscription_id=str(subscription_id))
        available_offers = customer_portal.get_available_portal_offers(db)
        current_offer = db.get(CatalogOffer, subscription.offer_id) if subscription and subscription.offer_id else None
        next_billing_date = _resolve_next_billing_date(db, subscription)

        return templates.TemplateResponse(
            "customer/services/change_plan.html",
            {
                "request": request,
                "customer": customer,
                "subscription": subscription,
                "current_offer": current_offer,
                "available_offers": available_offers,
                "next_billing_date": next_billing_date,
                "error": str(exc),
                "active_page": "services",
            },
            status_code=400,
        )


@router.get("/change-requests", response_class=HTMLResponse)
def customer_change_requests(
    request: Request,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
):
    """List pending plan change requests."""
    from app.services import subscription_changes as change_service

    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/change-requests", status_code=303)

    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    if not account_id_str:
        return templates.TemplateResponse(
            "customer/services/change_requests.html",
            {
                "request": request,
                "customer": customer,
                "change_requests": [],
                "status": status,
                "page": page,
                "per_page": per_page,
                "total": 0,
                "total_pages": 1,
                "active_page": "services",
            },
        )

    change_requests = change_service.subscription_change_requests.list(
        db=db,
        subscription_id=None,
        account_id=account_id_str,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    all_requests = change_service.subscription_change_requests.list(
        db=db,
        subscription_id=None,
        account_id=account_id_str,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_requests)
    total_pages = (total + per_page - 1) // per_page if total else 1

    return templates.TemplateResponse(
        "customer/services/change_requests.html",
        {
            "request": request,
            "customer": customer,
            "change_requests": change_requests,
            "status": status,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "active_page": "services",
        },
    )


# =============================================================================
# Payment Arrangements Self-Service
# =============================================================================

@router.get("/billing/arrangements", response_class=HTMLResponse)
def customer_payment_arrangements(
    request: Request,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
):
    """List payment arrangements for the customer."""
    from app.services import payment_arrangements as arrangement_service

    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal/billing/arrangements", status_code=303)

    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    if not account_id_str:
        return templates.TemplateResponse(
            "customer/billing/arrangements.html",
            {
                "request": request,
                "customer": customer,
                "arrangements": [],
                "status": status,
                "page": page,
                "per_page": per_page,
                "total": 0,
                "total_pages": 1,
                "active_page": "billing",
            },
        )

    arrangements = arrangement_service.payment_arrangements.list(
        db=db,
        account_id=account_id_str,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    all_arrangements = arrangement_service.payment_arrangements.list(
        db=db,
        account_id=account_id_str,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_arrangements)
    total_pages = (total + per_page - 1) // per_page if total else 1

    return templates.TemplateResponse(
        "customer/billing/arrangements.html",
        {
            "request": request,
            "customer": customer,
            "arrangements": arrangements,
            "status": status,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "active_page": "billing",
        },
    )


@router.get("/billing/arrangements/new", response_class=HTMLResponse)
def customer_new_payment_arrangement(
    request: Request,
    invoice_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Show form to request a new payment arrangement."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    # Get outstanding balance from service
    invoices = []
    outstanding_balance = 0
    if account_id_str:
        balance_data = customer_portal.get_outstanding_balance(db, account_id_str)
        invoices = balance_data["invoices"]
        outstanding_balance = balance_data["outstanding_balance"]

    # Pre-select invoice if provided
    selected_invoice = None
    if invoice_id:
        selected_invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)

    return templates.TemplateResponse(
        "customer/billing/arrangement_form.html",
        {
            "request": request,
            "customer": customer,
            "invoices": invoices,
            "selected_invoice": selected_invoice,
            "outstanding_balance": outstanding_balance,
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
):
    """Submit a payment arrangement request."""
    from app.services import payment_arrangements as arrangement_service
    from datetime import datetime
    from decimal import Decimal

    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    # Get subscriber for person_id
    subscriber_id = customer.get("subscriber_id")
    subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None
    person_id = str(subscriber.person_id) if subscriber and subscriber.person_id else None

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        amount = Decimal(total_amount.replace(",", ""))

        arrangement_service.payment_arrangements.create(
            db=db,
            account_id=account_id_str,
            total_amount=amount,
            installments=installments,
            frequency=frequency,
            start_date=start,
            invoice_id=invoice_id if invoice_id else None,
            requested_by_person_id=person_id,
            notes=notes,
        )
        return RedirectResponse(
            url="/portal/billing/arrangements?submitted=true",
            status_code=303,
        )
    except Exception as exc:
        invoices = billing_service.invoices.list(
            db=db,
            account_id=account_id_str,
            status="overdue",
            is_active=True,
            order_by="due_at",
            order_dir="asc",
            limit=50,
            offset=0,
        )
        outstanding_balance = sum(inv.balance_due or 0 for inv in invoices)

        return templates.TemplateResponse(
            "customer/billing/arrangement_form.html",
            {
                "request": request,
                "customer": customer,
                "invoices": invoices,
                "outstanding_balance": outstanding_balance,
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
):
    """View payment arrangement details."""
    from app.services import payment_arrangements as arrangement_service

    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    account_id = customer.get("account_id")

    arrangement = arrangement_service.payment_arrangements.get(db=db, arrangement_id=str(arrangement_id))
    if not arrangement:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Payment arrangement not found"},
            status_code=404,
        )

    # Verify arrangement belongs to customer
    if account_id and str(arrangement.account_id) != str(account_id):
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Payment arrangement not found"},
            status_code=404,
        )

    # Get installments
    installments = arrangement_service.installments.list(
        db=db,
        arrangement_id=str(arrangement_id),
        status=None,
        order_by="installment_number",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    return templates.TemplateResponse(
        "customer/billing/arrangement_detail.html",
        {
            "request": request,
            "customer": customer,
            "arrangement": arrangement,
            "installments": installments,
            "active_page": "billing",
        },
    )
