"""Admin billing management web routes."""

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional
from uuid import UUID

from app.db import SessionLocal
from app.models.catalog import BillingCycle
from app.models.billing import (
    CollectionAccountType,
    CreditNote,
    CreditNoteStatus,
    LedgerEntry,
    LedgerEntryType,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentChannelType,
    PaymentMethod,
    PaymentMethodType,
)
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber
from app.services import audit as audit_service
from app.services import billing as billing_service
from app.services import settings_spec
from app.services.audit_helpers import build_changes_metadata, extract_changes, format_changes, log_audit_event
from app.services import billing_automation as billing_automation_service
from app.services.billing import configuration as billing_config_service
from app.services import collections as collections_service
from app.services import subscriber as subscriber_service
from app.services.common import validate_enum
from app.schemas.billing import (
    CollectionAccountUpdate,
    CreditNoteApplyRequest,
    CreditNoteCreate,
    InvoiceCreate,
    InvoiceLineCreate,
    InvoiceLineUpdate,
    InvoiceUpdate,
    LedgerEntryCreate,
    PaymentCreate,
    PaymentMethodCreate,
    PaymentUpdate,
    TaxRateCreate,
)
from app.schemas.subscriber import SubscriberAccountCreate
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _placeholder_context(request: Request, db: Session, title: str, active_page: str):
    from app.web.admin import get_sidebar_stats, get_current_user
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "billing",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "page_title": title,
        "heading": title,
        "description": f"{title} management will appear here.",
        "empty_title": f"No {title.lower()} yet",
        "empty_message": "Billing configuration will appear once it is enabled.",
    }


def _parse_uuid(value: str | None, field: str):
    if not value:
        raise ValueError(f"{field} is required")
    return UUID(value)


def _parse_decimal(value: str | None, field: str, default: Decimal | None = None) -> Decimal:
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError(f"{field} is required")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a valid number") from exc


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _serialize_tax_rates(tax_rates: list) -> list[dict]:
    serialized = []
    for rate in tax_rates or []:
        serialized.append(
            {
                "id": str(rate.id),
                "name": rate.name,
                "rate": float(rate.rate or 0),
            }
        )
    return serialized


def _account_label(account) -> str:
    if not account:
        return "Account"
    if getattr(account, "subscriber", None):
        subscriber = account.subscriber
        if getattr(subscriber, "person", None):
            person = subscriber.person
            if getattr(person, "display_name", None):
                return person.display_name
            first = person.first_name or ""
            last = person.last_name or ""
            label = f"{first} {last}".strip()
            if label:
                return label
        if getattr(subscriber, "organization", None):
            name = subscriber.organization.name or ""
            if name:
                return name
    if getattr(account, "account_number", None):
        return f"Account {account.account_number}"
    return "Account"


def _parse_customer_ref(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    raw = value.strip()
    if ":" in raw:
        kind, ref_id = raw.split(":", 1)
        return kind, ref_id
    return None, raw


def _subscriber_label(subscriber) -> str:
    if not subscriber:
        return "Subscriber"
    person = getattr(subscriber, "person", None)
    if person:
        if getattr(person, "organization", None):
            return person.organization.name
        name = " ".join(part for part in [person.first_name, person.last_name] if part)
        return name or "Subscriber"
    return "Subscriber"


def _customer_label(db: Session, customer_ref: str | None) -> str | None:
    kind, ref_id = _parse_customer_ref(customer_ref)
    if not ref_id:
        return None
    try:
        if kind == "organization":
            from app.models.subscriber import Organization
            organization = db.get(Organization, ref_id)
            if organization:
                return organization.name
        else:
            from app.models.subscriber import Subscriber
            subscriber = db.get(Subscriber, ref_id)
            if subscriber:
                label = " ".join(part for part in [subscriber.first_name, subscriber.last_name] if part)
                return label or None
    except Exception:
        return None
    return None


def _subscriber_ids_for_customer(db: Session, customer_ref: str | None) -> list[str]:
    from app.services import subscriber as subscriber_service
    kind, ref_id = _parse_customer_ref(customer_ref)
    if not ref_id:
        return []
    subscribers = []
    if kind == "organization":
        subscribers = subscriber_service.subscribers.list(
            db=db,
            person_id=None,
            organization_id=ref_id,
            subscriber_type=None,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
    else:
        subscribers = subscriber_service.subscribers.list(
            db=db,
            person_id=ref_id,
            organization_id=None,
            subscriber_type=None,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
    return [str(sub.id) for sub in subscribers or []]


def _accounts_for_customer(db: Session, customer_ref: str | None) -> list[dict]:
    from app.services import subscriber as subscriber_service
    kind, ref_id = _parse_customer_ref(customer_ref)
    if not ref_id:
        return []
    subscribers = []
    if kind == "organization":
        subscribers = subscriber_service.subscribers.list(
            db=db,
            person_id=None,
            organization_id=ref_id,
            subscriber_type=None,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
    else:
        subscribers = subscriber_service.subscribers.list(
            db=db,
            person_id=ref_id,
            organization_id=None,
            subscriber_type=None,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
    accounts = []
    for subscriber in subscribers or []:
        for account in getattr(subscriber, "accounts", []) or []:
            accounts.append(
                {
                    "id": str(account.id),
                    "label": _account_label(account),
                    "account_number": account.account_number,
                }
            )
    return accounts


def _subscribers_for_customer(db: Session, customer_ref: str | None) -> list[dict]:
    from app.services import subscriber as subscriber_service
    kind, ref_id = _parse_customer_ref(customer_ref)
    if not ref_id:
        return []
    subscribers = []
    if kind == "organization":
        subscribers = subscriber_service.subscribers.list(
            db=db,
            person_id=None,
            organization_id=ref_id,
            subscriber_type=None,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
    else:
        subscribers = subscriber_service.subscribers.list(
            db=db,
            person_id=ref_id,
            organization_id=None,
            subscriber_type=None,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
    return [
        {
            "id": str(sub.id),
            "label": _subscriber_label(sub),
        }
        for sub in subscribers or []
    ]


def _invoice_status_value(invoice) -> str:
    status = getattr(invoice, "status", None)
    return status.value if hasattr(status, "value") else str(status or "")


def _filter_open_invoices(invoices: list) -> list:
    return [
        invoice
        for invoice in invoices
        if _invoice_status_value(invoice) in {"issued", "partially_paid", "overdue"}
    ]


def _resolve_invoice(db: Session, invoice_id: str | None):
    if not invoice_id:
        return None
    try:
        return billing_service.invoices.get(db=db, invoice_id=invoice_id)
    except Exception:
        return None


def _invoice_balance_info(invoice) -> tuple[str | None, str | None]:
    if not invoice:
        return None, None
    balance = invoice.balance_due if invoice.balance_due else invoice.total
    if balance is None:
        return None, None
    value = f"{balance:.2f}"
    display = f"{invoice.currency} {balance:,.2f}"
    return value, display


def _build_invoice_form_config(
    invoice,
    tax_rates_json: list | None,
    existing_line_items: list | None,
    payment_terms_days: int = 30,
) -> dict:
    currency = ""
    if invoice and invoice.currency:
        currency = str(invoice.currency).upper()
    return {
        "accountId": str(invoice.account_id) if invoice and invoice.account_id else "",
        "invoiceNumber": invoice.invoice_number if invoice and invoice.invoice_number else "",
        "status": invoice.status.value if invoice and invoice.status else "draft",
        "currency": currency or "NGN",
        "issuedAt": invoice.issued_at.strftime("%Y-%m-%d") if invoice and invoice.issued_at else "",
        "dueAt": invoice.due_at.strftime("%Y-%m-%d") if invoice and invoice.due_at else "",
        "memo": invoice.memo if invoice and invoice.memo else "",
        "taxRates": tax_rates_json or [],
        "lineItems": existing_line_items or [],
        "invoiceId": str(invoice.id) if invoice else "",
        "paymentTermsDays": payment_terms_days,
    }


def _payment_method_type_options() -> list[str]:
    spec = settings_spec.get_spec(SettingDomain.billing, "default_payment_method_type")
    if not spec or not spec.allowed:
        return []
    return sorted(spec.allowed)


def _resolve_payment_method_id(
    db: Session, account_id: UUID, selection: str | None
) -> UUID | None:
    if not selection:
        return None
    if selection.startswith("id:"):
        return UUID(selection.split(":", 1)[1])
    if selection.startswith("type:"):
        method_type = selection.split(":", 1)[1]
        allowed = _payment_method_type_options()
        if allowed and method_type not in allowed:
            raise ValueError("payment_method_type is invalid")
        method_type_enum = PaymentMethodType(method_type)
        method = (
            db.query(PaymentMethod)
            .filter(PaymentMethod.account_id == account_id)
            .filter(PaymentMethod.method_type == method_type_enum)
            .filter(PaymentMethod.is_active.is_(True))
            .order_by(PaymentMethod.created_at.desc())
            .first()
        )
        if method:
            return method.id
        label = method_type.replace("_", " ").title()
        payload = PaymentMethodCreate(
            account_id=account_id,
            method_type=method_type_enum,
            label=label,
        )
        method = billing_service.payment_methods.create(db, payload)
        return method.id
    return UUID(selection)


def _parse_billing_cycle(value: str | None) -> BillingCycle | None:
    if not value:
        return None
    try:
        return BillingCycle(value)
    except ValueError as exc:
        raise ValueError("Invalid billing cycle") from exc


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def billing_overview(
    request: Request,
    db: Session = Depends(get_db),
):
    """Billing overview page."""
    invoices = billing_service.invoices.list(
        db=db,
        account_id=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )

    # Calculate summary stats
    all_invoices = billing_service.invoices.list(
        db=db,
        account_id=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )

    # Helper to get status value (handles both enum and string)
    def get_status(inv):
        status = getattr(inv, "status", "")
        return status.value if hasattr(status, "value") else str(status)

    total_revenue = sum(
        float(getattr(inv, "total", 0) or 0)
        for inv in all_invoices
        if get_status(inv) == "paid"
    )
    pending_amount = sum(
        float(getattr(inv, "total", 0) or 0)
        for inv in all_invoices
        if get_status(inv) in ("pending", "sent")
    )
    overdue_amount = sum(
        float(getattr(inv, "total", 0) or 0)
        for inv in all_invoices
        if get_status(inv) == "overdue"
    )

    # Count invoices by status
    paid_count = sum(1 for inv in all_invoices if get_status(inv) == "paid")
    pending_count = sum(1 for inv in all_invoices if get_status(inv) in ("pending", "sent"))
    overdue_count = sum(1 for inv in all_invoices if get_status(inv) == "overdue")
    draft_count = sum(1 for inv in all_invoices if get_status(inv) == "draft")

    stats = {
        "total_revenue": total_revenue,
        "pending_amount": pending_amount,
        "overdue_amount": overdue_amount,
        "total_invoices": len(all_invoices),
        "paid_count": paid_count,
        "pending_count": pending_count,
        "overdue_count": overdue_count,
        "draft_count": draft_count,
    }

    accounts = subscriber_service.accounts.list(
        db=db,
        subscriber_id=None,
        reseller_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=2000,
        offset=0,
    )
    total_balance = sum((getattr(account, "balance", 0) or 0) for account in accounts)
    active_count = sum(
        1
        for account in accounts
        if (getattr(account.status, "value", account.status) or "active") == "active"
    )
    suspended_count = sum(
        1
        for account in accounts
        if (getattr(account.status, "value", account.status) or "") == "suspended"
    )

    # Get sidebar stats and current user
    from app.web.admin import get_sidebar_stats, get_current_user
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/billing/index.html",
        {
            "request": request,
            "invoices": invoices,
            "stats": stats,
            "total_balance": total_balance,
            "active_count": active_count,
            "suspended_count": suspended_count,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.get("/invoices", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoices_list(
    request: Request,
    account_id: Optional[str] = None,
    status: Optional[str] = None,
    customer_ref: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List all invoices with filtering."""
    offset = (page - 1) * per_page

    account_ids = []
    customer_filtered = bool(customer_ref)
    if customer_ref:
        account_ids = [UUID(item["id"]) for item in _accounts_for_customer(db, customer_ref)]
    invoices = []
    if account_ids:
        query = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(account_ids))
            .filter(Invoice.is_active.is_(True))
        )
        if status:
            query = query.filter(Invoice.status == validate_enum(status, InvoiceStatus, "status"))
        invoices = (
            query.order_by(Invoice.created_at.desc())
            .offset(offset)
            .limit(per_page)
            .all()
        )
    elif not customer_filtered:
        invoices = billing_service.invoices.list(
            db=db,
            account_id=UUID(account_id) if account_id else None,
            status=status if status else None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=per_page,
            offset=offset,
        )

    # Get total count
    if account_ids:
        count_query = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(account_ids))
            .filter(Invoice.is_active.is_(True))
        )
        if status:
            count_query = count_query.filter(Invoice.status == validate_enum(status, InvoiceStatus, "status"))
        total = count_query.count()
    elif not customer_filtered:
        all_invoices = billing_service.invoices.list(
            db=db,
            account_id=UUID(account_id) if account_id else None,
            status=status if status else None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=10000,
            offset=0,
        )
        total = len(all_invoices)
    else:
        total = 0
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "admin/billing/_invoices_table.html",
            {
                "request": request,
                "invoices": invoices,
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
            },
        )

    # Get sidebar stats and current user
    from app.web.admin import get_sidebar_stats, get_current_user
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/billing/invoices.html",
        {
            "request": request,
            "invoices": invoices,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "account_id": account_id,
            "status": status,
            "customer_ref": customer_ref,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.get("/invoices/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_new(
    request: Request,
    account_id: str | None = Query(None),
    account: str | None = Query(None),
    db: Session = Depends(get_db),
):
    resolved_account_id = account_id or account
    selected_account = None
    if resolved_account_id:
        try:
            selected_account = subscriber_service.accounts.get(db=db, account_id=resolved_account_id)
        except Exception:
            selected_account = None
    from datetime import timedelta
    accounts = None
    tax_rates = billing_service.tax_rates.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    tax_rates_json = _serialize_tax_rates(tax_rates)
    invoice_config = _build_invoice_form_config(None, tax_rates_json, [], 30)
    if selected_account:
        invoice_config["accountId"] = str(selected_account.id)

    # Get smart defaults from settings
    invoice_due_days = settings_spec.get_setting_value(
        db, SettingDomain.billing, "invoice_due_days", default=14
    )
    default_currency = settings_spec.get_setting_value(
        db, SettingDomain.billing, "default_currency", default="NGN"
    )
    today = datetime.now(timezone.utc).date()
    default_issue_date = today.strftime("%Y-%m-%d")
    default_due_date = (today + timedelta(days=invoice_due_days)).strftime("%Y-%m-%d")

    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/invoice_form.html",
        {
            "request": request,
            "accounts": accounts,
            "tax_rates": tax_rates,
            "tax_rates_json": tax_rates_json,
            "invoice_config": invoice_config,
            "invoice": None,
            "action_url": "/admin/billing/invoices/create",
            "form_title": "New Invoice",
            "submit_label": "Create Invoice",
            "show_line_items": True,
            "active_page": "invoices",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "default_issue_date": default_issue_date,
            "default_due_date": default_due_date,
            "default_currency": default_currency,
            "account_locked": bool(selected_account),
            "account_label": _account_label(selected_account) if selected_account else None,
            "account_number": selected_account.account_number if selected_account else None,
            "selected_account_id": str(selected_account.id) if selected_account else None,
            "account_x_model": "accountId",
        },
    )


@router.post("/invoices/create", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_create(
    request: Request,
    account_id: str = Form(...),
    invoice_number: str | None = Form(None),
    status: str | None = Form(None),
    currency: str = Form("NGN"),
    issued_at: str | None = Form(None),
    due_at: str | None = Form(None),
    memo: str | None = Form(None),
    line_description: list[str] = Form([]),
    line_quantity: list[str] = Form([]),
    line_unit_price: list[str] = Form([]),
    line_tax_rate_id: list[str] = Form([]),
    line_items_json: str | None = Form(None),
    issue_immediately: str | None = Form(None),
    send_notification: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        payload_data = {
            "account_id": _parse_uuid(account_id, "account_id"),
            "invoice_number": invoice_number.strip() if invoice_number else None,
            "status": status or "draft",
            "currency": currency.strip().upper(),
            "issued_at": _parse_datetime(issued_at),
            "due_at": _parse_datetime(due_at),
            "memo": memo.strip() if memo else None,
        }
        payload = InvoiceCreate(**payload_data)
        invoice = billing_service.invoices.create(db=db, payload=payload)
        line_items = []

        # Support both JSON format (new calculator) and array format (legacy)
        if line_items_json and line_items_json.strip():
            try:
                items_data = json.loads(line_items_json)
                for item in items_data:
                    description = item.get("description", "").strip()
                    if not description:
                        continue
                    line_items.append(
                        {
                            "description": description,
                            "quantity": Decimal(str(item.get("quantity", 1))),
                            "unit_price": Decimal(str(item.get("unitPrice", 0))),
                            "tax_rate_id": UUID(item["taxRateId"]) if item.get("taxRateId") else None,
                        }
                    )
            except (json.JSONDecodeError, KeyError, ValueError):
                pass  # Fall through to array format

        # Fallback to array format if JSON didn't produce items
        if not line_items:
            for idx, description in enumerate(line_description):
                if not description or not description.strip():
                    continue
                quantity_raw = line_quantity[idx] if idx < len(line_quantity) else ""
                unit_price_raw = line_unit_price[idx] if idx < len(line_unit_price) else ""
                tax_rate_raw = line_tax_rate_id[idx] if idx < len(line_tax_rate_id) else ""
                line_items.append(
                    {
                        "description": description.strip(),
                        "quantity": _parse_decimal(quantity_raw, "quantity", Decimal("1")),
                        "unit_price": _parse_decimal(
                            unit_price_raw, "unit_price", Decimal("0.00")
                        ),
                        "tax_rate_id": UUID(tax_rate_raw) if tax_rate_raw else None,
                    }
                )
        for item in line_items:
            billing_service.invoice_lines.create(
                db,
                InvoiceLineCreate(
                    invoice_id=invoice.id,
                    description=item["description"],
                    quantity=item["quantity"],
                    unit_price=item["unit_price"],
                    tax_rate_id=item["tax_rate_id"],
                ),
            )

        # Handle "issue immediately" option
        if issue_immediately:
            billing_service.invoices.update(
                db=db,
                invoice_id=str(invoice.id),
                payload=InvoiceUpdate(
                    status="issued",
                    issued_at=datetime.now(timezone.utc),
                ),
            )
            db.refresh(invoice)

        # Handle "send notification" option
        if send_notification and invoice.account:
            from app.services import email as email_service
            # Get subscriber email from account
            account = invoice.account
            if account.subscriber:
                subscriber = account.subscriber
                email_addr = None
                if subscriber.person and subscriber.person.email:
                    email_addr = subscriber.person.email
                elif subscriber.organization and subscriber.organization.email:
                    email_addr = subscriber.organization.email
                if email_addr:
                    email_service.send_email(
                        db=db,
                        to_email=email_addr,
                        subject=f"Invoice {invoice.invoice_number or invoice.id}",
                        template_name="invoice_notification",
                        context={
                            "invoice": invoice,
                            "account": account,
                        },
                    )
    except Exception as exc:
        selected_account = None
        if account_id:
            try:
                selected_account = subscriber_service.accounts.get(db=db, account_id=account_id)
            except Exception:
                selected_account = None
        accounts = None
        tax_rates = billing_service.tax_rates.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=200,
            offset=0,
        )
        tax_rates_json = _serialize_tax_rates(tax_rates)
        invoice_config = _build_invoice_form_config(None, tax_rates_json, [], 30)
        if selected_account:
            invoice_config["accountId"] = str(selected_account.id)
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/billing/invoice_form.html",
            {
                "request": request,
                "accounts": accounts,
                "tax_rates": tax_rates,
                "tax_rates_json": tax_rates_json,
                "invoice_config": invoice_config,
                "action_url": "/admin/billing/invoices/create",
                "form_title": "New Invoice",
                "submit_label": "Create Invoice",
                "error": str(exc),
                "active_page": "invoices",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "account_locked": bool(selected_account),
                "account_label": _account_label(selected_account) if selected_account else None,
                "account_number": selected_account.account_number if selected_account else None,
                "selected_account_id": str(selected_account.id) if selected_account else None,
                "account_x_model": "accountId",
            },
            status_code=400,
        )
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="invoice",
        entity_id=str(invoice.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"invoice_number": invoice.invoice_number},
    )
    return RedirectResponse(url=f"/admin/billing/invoices/{invoice.id}", status_code=303)


@router.get("/invoices/{invoice_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_edit(request: Request, invoice_id: UUID, db: Session = Depends(get_db)):
    invoice = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
    if not invoice:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )
    selected_account = invoice.account
    if not selected_account and invoice.account_id:
        try:
            selected_account = subscriber_service.accounts.get(db=db, account_id=str(invoice.account_id))
        except Exception:
            selected_account = None
    tax_rates = billing_service.tax_rates.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    existing_line_items = [
        {
            "id": str(line.id),
            "description": line.description,
            "quantity": float(line.quantity or 1),
            "unit_price": float(line.unit_price or 0),
            "tax_rate_id": str(line.tax_rate_id) if line.tax_rate_id else None,
        }
        for line in (invoice.lines or [])
        if getattr(line, "is_active", True)
    ]
    tax_rates_json = _serialize_tax_rates(tax_rates)
    invoice_config = _build_invoice_form_config(invoice, tax_rates_json, existing_line_items, 30)
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/invoice_form.html",
        {
            "request": request,
            "accounts": None,
            "tax_rates": tax_rates,
            "tax_rates_json": tax_rates_json,
            "existing_line_items": existing_line_items,
            "invoice_config": invoice_config,
            "invoice": invoice,
            "action_url": f"/admin/billing/invoices/{invoice_id}/edit",
            "form_title": "Edit Invoice",
            "submit_label": "Save Changes",
            "show_line_items": False,
            "active_page": "invoices",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "account_locked": True,
            "account_label": _account_label(selected_account),
            "account_number": selected_account.account_number if selected_account else None,
            "selected_account_id": str(selected_account.id) if selected_account else str(invoice.account_id),
            "account_x_model": "accountId",
        },
    )


@router.post("/invoices/{invoice_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_update(
    request: Request,
    invoice_id: UUID,
    account_id: str = Form(...),
    invoice_number: str | None = Form(None),
    status: str | None = Form(None),
    currency: str = Form("NGN"),
    issued_at: str | None = Form(None),
    due_at: str | None = Form(None),
    memo: str | None = Form(None),
    line_items_json: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        before = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
        payload_data = {
            "account_id": _parse_uuid(account_id, "account_id"),
            "invoice_number": invoice_number.strip() if invoice_number else None,
            "status": status or "draft",
            "currency": currency.strip().upper(),
            "issued_at": _parse_datetime(issued_at),
            "due_at": _parse_datetime(due_at),
            "memo": memo.strip() if memo else None,
        }
        payload = InvoiceUpdate(**payload_data)
        billing_service.invoices.update(db=db, invoice_id=str(invoice_id), payload=payload)
        if line_items_json and line_items_json.strip():
            try:
                items_data = json.loads(line_items_json)
            except json.JSONDecodeError:
                items_data = None
            if items_data is not None:
                existing_lines = {
                    str(line.id): line
                    for line in (before.lines or [])
                    if getattr(line, "is_active", True)
                }
                seen_ids: set[str] = set()
                for item in items_data:
                    description = str(item.get("description", "")).strip()
                    if not description:
                        continue
                    quantity = Decimal(str(item.get("quantity", 1)))
                    unit_price = Decimal(str(item.get("unitPrice", 0)))
                    tax_rate_id = item.get("taxRateId") or item.get("tax_rate_id")
                    line_id = item.get("id") or item.get("lineId") or item.get("line_id")
                    if line_id and str(line_id) in existing_lines:
                        seen_ids.add(str(line_id))
                        billing_service.invoice_lines.update(
                            db,
                            str(line_id),
                            InvoiceLineUpdate(
                                description=description,
                                quantity=quantity,
                                unit_price=unit_price,
                                tax_rate_id=UUID(tax_rate_id) if tax_rate_id else None,
                            ),
                        )
                    else:
                        billing_service.invoice_lines.create(
                            db,
                            InvoiceLineCreate(
                                invoice_id=invoice_id,
                                description=description,
                                quantity=quantity,
                                unit_price=unit_price,
                                tax_rate_id=UUID(tax_rate_id) if tax_rate_id else None,
                            ),
                        )
                for line_id in existing_lines:
                    if line_id not in seen_ids:
                        billing_service.invoice_lines.delete(db, line_id)
        after = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
        metadata_payload = build_changes_metadata(before, after)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="invoice",
            entity_id=str(invoice_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
    except Exception as exc:
        invoice = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
        selected_account = invoice.account if invoice else None
        if not selected_account and invoice and invoice.account_id:
            try:
                selected_account = subscriber_service.accounts.get(
                    db=db, account_id=str(invoice.account_id)
                )
            except Exception:
                selected_account = None
        tax_rates = billing_service.tax_rates.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=200,
            offset=0,
        )
        existing_line_items = [
            {
                "id": str(line.id),
                "description": line.description,
                "quantity": float(line.quantity or 1),
                "unit_price": float(line.unit_price or 0),
                "tax_rate_id": str(line.tax_rate_id) if line.tax_rate_id else None,
            }
            for line in (invoice.lines or [])
            if getattr(line, "is_active", True)
        ] if invoice else []
        tax_rates_json = _serialize_tax_rates(tax_rates)
        invoice_config = _build_invoice_form_config(invoice, tax_rates_json, existing_line_items, 30)
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/billing/invoice_form.html",
            {
                "request": request,
                "accounts": None,
                "tax_rates": tax_rates,
                "tax_rates_json": tax_rates_json,
                "existing_line_items": existing_line_items,
                "invoice_config": invoice_config,
                "invoice": invoice,
                "action_url": f"/admin/billing/invoices/{invoice_id}/edit",
                "form_title": "Edit Invoice",
                "submit_label": "Save Changes",
                "show_line_items": False,
                "error": str(exc),
                "active_page": "invoices",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "account_locked": True,
                "account_label": _account_label(selected_account),
                "account_number": selected_account.account_number if selected_account else None,
                "selected_account_id": str(selected_account.id) if selected_account else str(invoice.account_id) if invoice else None,
                "account_x_model": "accountId",
            },
            status_code=400,
        )
    return RedirectResponse(url=f"/admin/billing/invoices/{invoice_id}", status_code=303)


@router.get("/invoices/search", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_search(request: Request, db: Session = Depends(get_db)):
    return HTMLResponse("")


@router.get("/invoices/filter", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_filter(request: Request, db: Session = Depends(get_db)):
    return HTMLResponse("")


@router.post("/invoices/generate-batch", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_generate_batch(
    request: Request,
    billing_cycle: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        summary = billing_automation_service.run_invoice_cycle(
            db=db,
            billing_cycle=_parse_billing_cycle(billing_cycle),
            dry_run=False,
        )
        note = f"Batch run completed. Invoices created: {summary.get('invoices_created', 0)}."
    except Exception as exc:
        note = f"Batch run failed: {exc}"
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"{note}"
        "</div>"
    )


@router.get("/invoices/batch", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_batch(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user
    today = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    return templates.TemplateResponse(
        "admin/billing/invoice_batch.html",
        {
            "request": request,
            "active_page": "invoices",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "today": today,
        },
    )


@router.post("/invoices/generate-batch/preview", dependencies=[Depends(require_permission("billing:read"))])
def invoice_generate_batch_preview(
    request: Request,
    billing_cycle: str | None = Form(None),
    subscription_status: str | None = Form(None),
    billing_date: str | None = Form(None),
    invoice_status: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Dry-run preview of batch invoice generation."""
    from fastapi.responses import JSONResponse
    from decimal import Decimal

    try:
        # Parse billing date
        run_date = None
        if billing_date:
            run_date = datetime.strptime(billing_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        summary = billing_automation_service.run_invoice_cycle(
            db=db,
            billing_cycle=_parse_billing_cycle(billing_cycle),
            dry_run=True,
            run_date=run_date,
        )

        # Format response for preview
        total_amount = summary.get("total_amount", Decimal("0.00"))
        subscriptions = summary.get("subscriptions", [])

        return JSONResponse({
            "invoice_count": summary.get("invoices_created", 0),
            "account_count": summary.get("accounts_affected", len(set(s.get("account_id") for s in subscriptions))),
            "total_amount": float(total_amount),
            "total_amount_formatted": f"NGN {total_amount:,.2f}",
            "subscriptions": [
                {
                    "id": str(s.get("id", "")),
                    "offer_name": s.get("offer_name", "Unknown"),
                    "amount": float(s.get("amount", 0)),
                    "amount_formatted": f"NGN {s.get('amount', 0):,.2f}",
                }
                for s in subscriptions[:50]  # Limit to first 50 for preview
            ],
        })
    except Exception as exc:
        return JSONResponse(
            {"error": str(exc), "invoice_count": 0, "account_count": 0, "total_amount_formatted": "NGN 0.00"},
            status_code=400,
        )


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_detail(
    request: Request,
    invoice_id: UUID,
    db: Session = Depends(get_db),
):
    """View invoice details."""
    invoice = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
    if not invoice:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )

    # Get sidebar stats and current user
    from app.web.admin import get_sidebar_stats, get_current_user
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)
    tax_rates = billing_service.tax_rates.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    credit_notes = (
        db.query(CreditNote)
        .filter(CreditNote.account_id == invoice.account_id)
        .filter(CreditNote.is_active.is_(True))
        .filter(
            CreditNote.status.in_(
                [CreditNoteStatus.issued, CreditNoteStatus.partially_applied]
            )
        )
        .order_by(CreditNote.created_at.desc())
        .all()
    )
    audit_events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type="invoice",
        entity_id=str(invoice_id),
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    actor_ids = {str(event.actor_id) for event in audit_events if getattr(event, "actor_id", None)}
    people = {}
    if actor_ids:
        people = {
            str(person.id): person
            for person in db.query(Subscriber).filter(Subscriber.id.in_(actor_ids)).all()
        }
    activities = []
    for event in audit_events:
        actor = people.get(str(event.actor_id)) if getattr(event, "actor_id", None) else None
        actor_name = f"{actor.first_name} {actor.last_name}".strip() if actor else "System"
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        activities.append(
            {
                "title": (event.action or "Activity").replace("_", " ").title(),
                "description": f"{actor_name}" + (f" · {change_summary}" if change_summary else ""),
                "occurred_at": event.occurred_at,
            }
        )

    return templates.TemplateResponse(
        "admin/billing/invoice_detail.html",
        {
            "request": request,
            "invoice": invoice,
            "tax_rates": tax_rates,
            "credit_notes": credit_notes,
            "activities": activities,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.post("/invoices/{invoice_id}/lines", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_line_create(
    request: Request,
    invoice_id: UUID,
    description: str = Form(...),
    quantity: str = Form("1"),
    unit_price: str = Form("0"),
    tax_rate_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        payload = InvoiceLineCreate(
            invoice_id=_parse_uuid(str(invoice_id), "invoice_id"),
            description=description.strip(),
            quantity=_parse_decimal(quantity, "quantity"),
            unit_price=_parse_decimal(unit_price, "unit_price"),
            tax_rate_id=UUID(tax_rate_id) if tax_rate_id else None,
        )
        billing_service.invoice_lines.create(db, payload)
    except Exception as exc:
        invoice = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
        from app.web.admin import get_sidebar_stats, get_current_user
        tax_rates = billing_service.tax_rates.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=200,
            offset=0,
        )
        return templates.TemplateResponse(
            "admin/billing/invoice_detail.html",
            {
                "request": request,
                "invoice": invoice,
                "tax_rates": tax_rates,
                "error": str(exc),
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url=f"/admin/billing/invoices/{invoice_id}", status_code=303)


@router.post("/invoices/{invoice_id}/apply-credit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_apply_credit(
    request: Request,
    invoice_id: UUID,
    credit_note_id: str = Form(...),
    amount: str | None = Form(None),
    memo: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        before = billing_service.credit_notes.get(db=db, credit_note_id=credit_note_id)
        payload = CreditNoteApplyRequest(
            invoice_id=_parse_uuid(str(invoice_id), "invoice_id"),
            amount=_parse_decimal(amount, "amount") if amount else None,
            memo=memo.strip() if memo else None,
        )
        billing_service.credit_notes.apply(db, credit_note_id, payload)
        after = billing_service.credit_notes.get(db=db, credit_note_id=credit_note_id)
        metadata_payload = build_changes_metadata(before, after)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="apply",
            entity_type="credit_note",
            entity_id=str(credit_note_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
    except Exception as exc:
        invoice = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
        from app.web.admin import get_sidebar_stats, get_current_user
        tax_rates = billing_service.tax_rates.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=200,
            offset=0,
        )
        credit_notes = (
            db.query(CreditNote)
            .filter(CreditNote.account_id == invoice.account_id)
            .filter(CreditNote.is_active.is_(True))
            .filter(
                CreditNote.status.in_(
                    [CreditNoteStatus.issued, CreditNoteStatus.partially_applied]
                )
            )
            .order_by(CreditNote.created_at.desc())
            .all()
        )
        return templates.TemplateResponse(
            "admin/billing/invoice_detail.html",
            {
                "request": request,
                "invoice": invoice,
                "tax_rates": tax_rates,
                "credit_notes": credit_notes,
                "error": str(exc),
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url=f"/admin/billing/invoices/{invoice_id}", status_code=303)


@router.get("/invoices/{invoice_id}/pdf", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_pdf(request: Request, invoice_id: UUID, db: Session = Depends(get_db)):
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"PDF generation queued for invoice {invoice_id}."
        "</div>"
    )


@router.post("/invoices/{invoice_id}/send", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_send(request: Request, invoice_id: UUID, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="send",
        entity_type="invoice",
        entity_id=str(invoice_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"Invoice {invoice_id} send queued."
        "</div>"
    )


@router.post("/invoices/{invoice_id}/void", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_void(request: Request, invoice_id: UUID, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="void",
        entity_type="invoice",
        entity_id=str(invoice_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"Invoice {invoice_id} void queued."
        "</div>"
    )


@router.post("/invoices/bulk/issue", dependencies=[Depends(require_permission("billing:write"))])
def invoice_bulk_issue(
    request: Request,
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    """Bulk issue invoices (change status from draft to issued)."""
    from fastapi.responses import JSONResponse
    from app.models.billing import InvoiceStatus
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    count = 0

    for invoice_id in invoice_ids.split(","):
        invoice_id = invoice_id.strip()
        if not invoice_id:
            continue
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if invoice and invoice.status == InvoiceStatus.draft:
                invoice.status = InvoiceStatus.issued
                invoice.issued_at = datetime.now(timezone.utc)
                db.commit()
                log_audit_event(
                    db=db,
                    request=request,
                    action="issue",
                    entity_type="invoice",
                    entity_id=invoice_id,
                    actor_id=str(current_user.get("subscriber_id")) if current_user else None,
                )
                count += 1
        except Exception:
            continue

    return JSONResponse({"message": f"Issued {count} invoices", "count": count})


@router.post("/invoices/bulk/send", dependencies=[Depends(require_permission("billing:write"))])
def invoice_bulk_send(
    request: Request,
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    """Bulk send invoice notifications."""
    from fastapi.responses import JSONResponse
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    count = 0

    for invoice_id in invoice_ids.split(","):
        invoice_id = invoice_id.strip()
        if not invoice_id:
            continue
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if invoice:
                # TODO: Queue email send task
                log_audit_event(
                    db=db,
                    request=request,
                    action="send",
                    entity_type="invoice",
                    entity_id=invoice_id,
                    actor_id=str(current_user.get("subscriber_id")) if current_user else None,
                )
                count += 1
        except Exception:
            continue

    return JSONResponse({"message": f"Queued {count} invoice notifications", "count": count})


@router.post("/invoices/bulk/void", dependencies=[Depends(require_permission("billing:write"))])
def invoice_bulk_void(
    request: Request,
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    """Bulk void invoices."""
    from fastapi.responses import JSONResponse
    from app.models.billing import InvoiceStatus
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    count = 0

    for invoice_id in invoice_ids.split(","):
        invoice_id = invoice_id.strip()
        if not invoice_id:
            continue
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if invoice and invoice.status not in [InvoiceStatus.paid, InvoiceStatus.void]:
                invoice.status = InvoiceStatus.void
                db.commit()
                log_audit_event(
                    db=db,
                    request=request,
                    action="void",
                    entity_type="invoice",
                    entity_id=invoice_id,
                    actor_id=str(current_user.get("subscriber_id")) if current_user else None,
                )
                count += 1
        except Exception:
            continue

    return JSONResponse({"message": f"Voided {count} invoices", "count": count})


@router.get("/credits", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def billing_credits_list(
    request: Request,
    page: int = 1,
    status: str | None = None,
    customer_ref: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """List all credit notes."""
    from app.web.admin import get_sidebar_stats, get_current_user

    per_page = 50
    offset = (page - 1) * per_page

    account_ids = []
    customer_filtered = bool(customer_ref)
    if customer_ref:
        account_ids = [UUID(item["id"]) for item in _accounts_for_customer(db, customer_ref)]

    # Get status counts for filter badges
    if customer_filtered and not account_ids:
        status_counts = {
            "draft": 0,
            "issued": 0,
            "partially_applied": 0,
            "applied": 0,
            "void": 0,
        }
    else:
        status_query = db.query(CreditNote)
        if account_ids:
            status_query = status_query.filter(CreditNote.account_id.in_(account_ids))
        status_counts = {
            "draft": status_query.filter(CreditNote.status == CreditNoteStatus.draft).count(),
            "issued": status_query.filter(CreditNote.status == CreditNoteStatus.issued).count(),
            "partially_applied": status_query.filter(CreditNote.status == CreditNoteStatus.partially_applied).count(),
            "applied": status_query.filter(CreditNote.status == CreditNoteStatus.applied).count(),
            "void": status_query.filter(CreditNote.status == CreditNoteStatus.void).count(),
        }

    # Build query
    query = db.query(CreditNote).filter(CreditNote.is_active.is_(True))
    credits = []
    total = 0
    total_pages = 1
    if account_ids:
        query = query.filter(CreditNote.account_id.in_(account_ids))
    if not customer_filtered or account_ids:
        if status:
            query = query.filter(CreditNote.status == status)
        total = query.count()
        total_pages = (total + per_page - 1) // per_page if total > 0 else 1
        credits = (
            query.order_by(CreditNote.created_at.desc()).offset(offset).limit(per_page).all()
        )

    return templates.TemplateResponse(
        "admin/billing/credits.html",
        {
            "request": request,
            "credits": credits,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "status": status,
            "status_counts": status_counts,
            "customer_ref": customer_ref,
            "active_page": "credits",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/credits/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def billing_credit_new(
    request: Request,
    account_id: str | None = Query(None),
    account: str | None = Query(None),
    db: Session = Depends(get_db),
):
    resolved_account_id = account_id or account
    selected_account = None
    if resolved_account_id:
        try:
            selected_account = subscriber_service.accounts.get(db=db, account_id=resolved_account_id)
        except Exception:
            selected_account = None
    accounts = None
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/credit_form.html",
        {
            "request": request,
            "accounts": accounts,
            "action_url": "/admin/billing/credits",
            "form_title": "Issue Credit",
            "submit_label": "Issue Credit",
            "active_page": "credits",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "account_locked": bool(selected_account),
            "account_label": _account_label(selected_account) if selected_account else None,
            "account_number": selected_account.account_number if selected_account else None,
            "selected_account_id": str(selected_account.id) if selected_account else None,
        },
    )


@router.post("/credits", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def billing_credit_create(
    request: Request,
    account_id: str = Form(...),
    amount: str = Form(...),
    currency: str = Form("NGN"),
    memo: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        credit_amount = _parse_decimal(amount, "amount")
        payload = CreditNoteCreate(
            account_id=_parse_uuid(account_id, "account_id"),
            status="issued",
            currency=currency.strip().upper(),
            subtotal=credit_amount,
            tax_total=Decimal("0.00"),
            total=credit_amount,
            memo=memo.strip() if memo else None,
        )
        billing_service.credit_notes.create(db, payload)
    except Exception as exc:
        selected_account = None
        if account_id:
            try:
                selected_account = subscriber_service.accounts.get(db=db, account_id=account_id)
            except Exception:
                selected_account = None
        accounts = None
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/billing/credit_form.html",
            {
                "request": request,
                "accounts": accounts,
                "action_url": "/admin/billing/credits",
                "form_title": "Issue Credit",
                "submit_label": "Issue Credit",
                "error": str(exc),
                "active_page": "credits",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "account_locked": bool(selected_account),
                "account_label": _account_label(selected_account) if selected_account else None,
                "account_number": selected_account.account_number if selected_account else None,
                "selected_account_id": str(selected_account.id) if selected_account else None,
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/billing/invoices", status_code=303)


@router.get("/payments", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def payments_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    customer_ref: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """List all payments."""
    offset = (page - 1) * per_page
    account_ids = []
    customer_filtered = bool(customer_ref)
    if customer_ref:
        account_ids = [UUID(item["id"]) for item in _accounts_for_customer(db, customer_ref)]
    payments = []
    total = 0
    if account_ids:
        query = (
            db.query(Payment)
            .filter(Payment.account_id.in_(account_ids))
            .filter(Payment.is_active.is_(True))
            .order_by(Payment.created_at.desc())
        )
        total = query.count()
        payments = query.offset(offset).limit(per_page).all()
    elif not customer_filtered:
        payments = billing_service.payments.list(
            db=db,
            account_id=None,
            invoice_id=None,
            status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=per_page,
            offset=offset,
        )
        all_payments = billing_service.payments.list(
            db=db,
            account_id=None,
            invoice_id=None,
            status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=10000,
            offset=0,
        )
        total = len(all_payments)
    else:
        total = 0
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    accounts = subscriber_service.accounts.list(
        db=db,
        subscriber_id=None,
        reseller_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=2000,
        offset=0,
    )
    total_balance = sum((getattr(account, "balance", 0) or 0) for account in accounts)
    active_count = sum(
        1
        for account in accounts
        if (getattr(account.status, "value", account.status) or "active") == "active"
    )
    suspended_count = sum(
        1
        for account in accounts
        if (getattr(account.status, "value", account.status) or "") == "suspended"
    )

    # Get sidebar stats and current user
    from app.web.admin import get_sidebar_stats, get_current_user
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/billing/payments.html",
        {
            "request": request,
            "payments": payments,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "total_balance": total_balance,
            "active_count": active_count,
            "suspended_count": suspended_count,
            "customer_ref": customer_ref,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.get("/payments/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def payment_new(
    request: Request,
    invoice_id: str | None = Query(None),
    invoice: str | None = Query(None),
    account_id: str | None = Query(None),
    account: str | None = Query(None),
    db: Session = Depends(get_db),
):
    prefill = {}
    resolved_invoice_id = invoice_id or invoice
    resolved_account_id = account_id or account
    selected_account = None
    invoice_label = None
    balance_value = None
    balance_display = None
    if resolved_invoice_id:
        try:
            invoice_obj = billing_service.invoices.get(
                db=db, invoice_id=resolved_invoice_id
            )
            prefill["invoice_id"] = str(invoice_obj.id)
            prefill["invoice_number"] = invoice_obj.invoice_number
            invoice_label = invoice_obj.invoice_number or "Invoice"
            balance_value, balance_display = _invoice_balance_info(invoice_obj)
            # Auto-fill amount from invoice balance_due
            if invoice_obj.balance_due:
                prefill["amount"] = float(invoice_obj.balance_due)
            elif invoice_obj.total:
                prefill["amount"] = float(invoice_obj.total)
            # Auto-fill currency from invoice
            if invoice_obj.currency:
                prefill["currency"] = invoice_obj.currency
            # Default status to succeeded for payment recording
            prefill["status"] = "succeeded"
            resolved_account_id = str(invoice_obj.account_id)
        except Exception:
            pass
    if resolved_account_id:
        prefill["account_id"] = resolved_account_id
        try:
            selected_account = subscriber_service.accounts.get(db=db, account_id=resolved_account_id)
        except Exception:
            selected_account = None
    accounts = None
    payment_methods = []
    payment_method_types = []
    collection_accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    # Get unpaid invoices for the dropdown
    invoices = []
    if selected_account:
        invoices = billing_service.invoices.list(
            db=db,
            account_id=str(selected_account.id),
            status=None,
            is_active=True,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
        invoices = _filter_open_invoices(invoices)
        if prefill.get("invoice_id"):
            try:
                invoice_obj = billing_service.invoices.get(
                    db=db, invoice_id=prefill["invoice_id"]
                )
                if all(str(inv.id) != str(invoice_obj.id) for inv in invoices):
                    invoices = [invoice_obj, *invoices]
            except Exception:
                pass
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/payment_form.html",
        {
            "request": request,
            "accounts": accounts,
            "payment_methods": payment_methods,
            "payment_method_types": payment_method_types,
            "collection_accounts": collection_accounts,
            "invoices": invoices,
            "prefill": prefill,
            "invoice_label": invoice_label,
            "action_url": "/admin/billing/payments/create",
            "form_title": "Record Payment",
            "submit_label": "Record Payment",
            "active_page": "payments",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "account_locked": bool(selected_account),
            "account_label": _account_label(selected_account) if selected_account else None,
            "account_number": selected_account.account_number if selected_account else None,
            "selected_account_id": str(selected_account.id) if selected_account else None,
            "currency_locked": bool(prefill.get("invoice_id")),
            "show_invoice_typeahead": not bool(selected_account),
            "selected_invoice_id": prefill.get("invoice_id"),
            "balance_value": balance_value,
            "balance_display": balance_display,
        },
    )


@router.get(
    "/payments/invoice-options",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_invoice_options(
    request: Request,
    account_id: str | None = Query(None),
    invoice_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    selected_account = None
    if account_id:
        try:
            selected_account = subscriber_service.accounts.get(db=db, account_id=account_id)
        except Exception:
            selected_account = None
    invoices = []
    if selected_account:
        invoices = billing_service.invoices.list(
            db=db,
            account_id=str(selected_account.id),
            status=None,
            is_active=True,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
        invoices = _filter_open_invoices(invoices)
    invoice_label = None
    if invoice_id:
        invoice_obj = _resolve_invoice(db, invoice_id)
        if invoice_obj:
            invoice_label = invoice_obj.invoice_number or "Invoice"
            if all(str(inv.id) != str(invoice_obj.id) for inv in invoices):
                invoices = [invoice_obj, *invoices]
    return templates.TemplateResponse(
        "admin/billing/_payment_invoice_select.html",
        {
            "request": request,
            "invoices": invoices,
            "selected_invoice_id": invoice_id,
            "invoice_label": invoice_label,
            "show_invoice_typeahead": not bool(selected_account),
        },
    )


@router.get(
    "/payments/invoice-currency",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_invoice_currency(
    request: Request,
    invoice_id: str | None = Query(None),
    currency: str | None = Query(None),
    db: Session = Depends(get_db),
):
    invoice_obj = _resolve_invoice(db, invoice_id)
    currency_value = currency or "NGN"
    currency_locked = False
    if invoice_obj and invoice_obj.currency:
        currency_value = invoice_obj.currency
        currency_locked = True
    return templates.TemplateResponse(
        "admin/billing/_payment_currency_field.html",
        {
            "request": request,
            "currency_value": currency_value,
            "currency_locked": currency_locked,
        },
    )


@router.get(
    "/payments/invoice-details",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_invoice_details(
    request: Request,
    invoice_id: str | None = Query(None),
    amount: str | None = Query(None),
    db: Session = Depends(get_db),
):
    invoice_obj = _resolve_invoice(db, invoice_id)
    amount_value = amount or ""
    balance_value = None
    balance_display = None
    if invoice_obj:
        balance_value, balance_display = _invoice_balance_info(invoice_obj)
        if balance_value:
            amount_value = balance_value
    return templates.TemplateResponse(
        "admin/billing/_payment_amount_field.html",
        {
            "request": request,
            "amount_value": amount_value,
            "balance_value": balance_value,
            "balance_display": balance_display,
        },
    )


@router.get(
    "/customer-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def billing_customer_accounts(
    request: Request,
    customer_ref: str | None = Query(None),
    account_id: str | None = Query(None),
    account_x_model: str | None = Query(None),
    db: Session = Depends(get_db),
):
    accounts = _accounts_for_customer(db, customer_ref)
    return templates.TemplateResponse(
        "admin/billing/_customer_account_select.html",
        {
            "request": request,
            "customer_ref": customer_ref,
            "accounts": accounts,
            "selected_account_id": account_id,
            "account_x_model": account_x_model,
        },
    )


@router.get(
    "/customer-subscribers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def billing_customer_subscribers(
    request: Request,
    customer_ref: str | None = Query(None),
    subscriber_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    subscribers = _subscribers_for_customer(db, customer_ref)
    return templates.TemplateResponse(
        "admin/billing/_customer_subscriber_select.html",
        {
            "request": request,
            "customer_ref": customer_ref,
            "subscribers": subscribers,
            "selected_subscriber_id": subscriber_id,
        },
    )


@router.post("/payments/create", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def payment_create(
    request: Request,
    account_id: str | None = Form(None),
    amount: str = Form(...),
    currency: str = Form("NGN"),
    status: str | None = Form(None),
    invoice_id: str | None = Form(None),
    collection_account_id: str | None = Form(None),
    memo: str | None = Form(None),
    db: Session = Depends(get_db),
):
    resolved_invoice = None
    balance_value = None
    balance_display = None
    try:
        resolved_invoice = _resolve_invoice(db, invoice_id)
        if resolved_invoice:
            balance_value, balance_display = _invoice_balance_info(resolved_invoice)
        resolved_account_id = account_id or (str(resolved_invoice.account_id) if resolved_invoice else None)
        if not resolved_account_id:
            raise ValueError("account_id is required")
        parsed_account_id = _parse_uuid(resolved_account_id, "account_id")
        if resolved_invoice and resolved_invoice.currency:
            currency = resolved_invoice.currency
        payload = PaymentCreate(
            account_id=parsed_account_id,
            invoice_id=UUID(invoice_id) if invoice_id else None,
            collection_account_id=UUID(collection_account_id)
            if collection_account_id
            else None,
            amount=_parse_decimal(amount, "amount"),
            currency=currency.strip().upper(),
            status=status or "pending",
            memo=memo.strip() if memo else None,
        )
        payment = billing_service.payments.create(db, payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="payment",
            entity_id=str(payment.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"amount": str(payment.amount), "invoice_id": str(payment.invoice_id) if payment.invoice_id else None},
        )
    except Exception as exc:
        selected_account = None
        if account_id or resolved_invoice:
            try:
                selected_account = subscriber_service.accounts.get(
                    db=db, account_id=account_id or str(resolved_invoice.account_id)
                )
            except Exception:
                selected_account = None
        accounts = None
        payment_methods = []
        payment_method_types = []
        collection_accounts = billing_service.collection_accounts.list(
            db=db,
            is_active=True,
            order_by="created_at",
            order_dir="desc",
            limit=500,
            offset=0,
        )
        invoices = []
        if selected_account:
            invoices = billing_service.invoices.list(
                db=db,
                account_id=str(selected_account.id),
                status=None,
                is_active=True,
                order_by="created_at",
                order_dir="desc",
                limit=200,
                offset=0,
            )
            invoices = _filter_open_invoices(invoices)
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/billing/payment_form.html",
            {
                "request": request,
                "accounts": accounts,
                "payment_methods": payment_methods,
                "payment_method_types": payment_method_types,
                "collection_accounts": collection_accounts,
                "invoices": invoices,
                "action_url": "/admin/billing/payments/create",
                "form_title": "Record Payment",
                "submit_label": "Record Payment",
                "error": str(exc),
                "active_page": "payments",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "account_locked": bool(selected_account),
                "account_label": _account_label(selected_account) if selected_account else None,
                "account_number": selected_account.account_number if selected_account else None,
                "selected_account_id": str(selected_account.id) if selected_account else None,
                "currency_locked": bool(resolved_invoice),
                "show_invoice_typeahead": not bool(selected_account),
                "selected_invoice_id": invoice_id,
                "balance_value": balance_value,
                "balance_display": balance_display,
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/billing/payments", status_code=303)


@router.get("/payments/{payment_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def payment_detail(request: Request, payment_id: UUID, db: Session = Depends(get_db)):
    payment = billing_service.payments.get(db=db, payment_id=str(payment_id))
    if not payment:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment not found"},
            status_code=404,
        )
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/payment_detail.html",
        {
            "request": request,
            "payment": payment,
            "active_page": "payments",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/payments/{payment_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def payment_edit(request: Request, payment_id: UUID, db: Session = Depends(get_db)):
    payment = billing_service.payments.get(db=db, payment_id=str(payment_id))
    if not payment:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment not found"},
            status_code=404,
        )
    selected_account = payment.account
    if not selected_account and payment.account_id:
        try:
            selected_account = subscriber_service.accounts.get(db=db, account_id=str(payment.account_id))
        except Exception:
            selected_account = None
    payment_methods = billing_service.payment_methods.list(
        db=db,
        account_id=str(payment.account_id),
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    payment_method_types = _payment_method_type_options()
    if payment.payment_method_id and all(
        str(method.id) != str(payment.payment_method_id) for method in payment_methods
    ):
        method = billing_service.payment_methods.get(db, str(payment.payment_method_id))
        if method:
            payment_methods = [*payment_methods, method]
    invoices = billing_service.invoices.list(
        db=db,
        account_id=str(selected_account.id) if selected_account else None,
        status=None,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=200,
        offset=0,
    )
    invoices = _filter_open_invoices(invoices)
    if payment.invoice_id:
        try:
            invoice_obj = billing_service.invoices.get(db=db, invoice_id=str(payment.invoice_id))
            if all(str(inv.id) != str(invoice_obj.id) for inv in invoices):
                invoices = [invoice_obj, *invoices]
        except Exception:
            pass
    balance_value, balance_display = _invoice_balance_info(payment.invoice if payment else None)
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/payment_form.html",
        {
            "request": request,
            "accounts": None,
            "payment_methods": payment_methods,
            "payment_method_types": payment_method_types,
            "invoices": invoices,
            "payment": payment,
            "action_url": f"/admin/billing/payments/{payment_id}/edit",
            "form_title": "Edit Payment",
            "submit_label": "Save Changes",
            "active_page": "payments",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "account_locked": True,
            "account_label": _account_label(selected_account),
            "account_number": selected_account.account_number if selected_account else None,
            "selected_account_id": str(selected_account.id) if selected_account else str(payment.account_id),
            "currency_locked": bool(payment.invoice_id),
            "show_invoice_typeahead": False,
            "selected_invoice_id": str(payment.invoice_id) if payment and payment.invoice_id else None,
            "balance_value": balance_value,
            "balance_display": balance_display,
        },
    )


@router.post("/payments/{payment_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def payment_update(
    request: Request,
    payment_id: UUID,
    account_id: str | None = Form(None),
    amount: str = Form(...),
    currency: str = Form("NGN"),
    status: str | None = Form(None),
    invoice_id: str | None = Form(None),
    payment_method_id: str | None = Form(None),
    memo: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        before = billing_service.payments.get(db=db, payment_id=str(payment_id))
        resolved_invoice = _resolve_invoice(db, invoice_id)
        resolved_account_id = account_id or (str(resolved_invoice.account_id) if resolved_invoice else None)
        if not resolved_account_id:
            resolved_account_id = str(before.account_id) if before else None
        if not resolved_account_id:
            raise ValueError("account_id is required")
        parsed_account_id = _parse_uuid(resolved_account_id, "account_id")
        if resolved_invoice and resolved_invoice.currency:
            currency = resolved_invoice.currency
        payload = PaymentUpdate(
            account_id=parsed_account_id,
            invoice_id=UUID(invoice_id) if invoice_id else None,
            payment_method_id=_resolve_payment_method_id(db, parsed_account_id, payment_method_id),
            amount=_parse_decimal(amount, "amount"),
            currency=currency.strip().upper(),
            status=status or "pending",
            memo=memo.strip() if memo else None,
        )
        billing_service.payments.update(db, str(payment_id), payload)
        after = billing_service.payments.get(db=db, payment_id=str(payment_id))
        metadata_payload = build_changes_metadata(before, after)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="payment",
            entity_id=str(payment_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
    except Exception as exc:
        payment = billing_service.payments.get(db=db, payment_id=str(payment_id))
        selected_account = payment.account if payment else None
        if not selected_account and payment and payment.account_id:
            try:
                selected_account = subscriber_service.accounts.get(
                    db=db, account_id=str(payment.account_id)
                )
            except Exception:
                selected_account = None
        payment_methods = billing_service.payment_methods.list(
            db=db,
            account_id=str(payment.account_id),
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=500,
            offset=0,
        )
        payment_method_types = _payment_method_type_options()
        if payment.payment_method_id and all(
            str(method.id) != str(payment.payment_method_id) for method in payment_methods
        ):
            method = billing_service.payment_methods.get(db, str(payment.payment_method_id))
            if method:
                payment_methods = [*payment_methods, method]
        invoices = billing_service.invoices.list(
            db=db,
            account_id=str(selected_account.id) if selected_account else None,
            status=None,
            is_active=True,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
        invoices = _filter_open_invoices(invoices)
        if payment and payment.invoice_id:
            try:
                invoice_obj = billing_service.invoices.get(
                    db=db, invoice_id=str(payment.invoice_id)
                )
                if all(str(inv.id) != str(invoice_obj.id) for inv in invoices):
                    invoices = [invoice_obj, *invoices]
            except Exception:
                pass
        balance_value, balance_display = _invoice_balance_info(payment.invoice if payment else None)
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/billing/payment_form.html",
            {
                "request": request,
                "accounts": None,
                "payment_methods": payment_methods,
                "payment_method_types": payment_method_types,
                "invoices": invoices,
                "payment": payment,
                "action_url": f"/admin/billing/payments/{payment_id}/edit",
                "form_title": "Edit Payment",
                "submit_label": "Save Changes",
                "error": str(exc),
                "active_page": "payments",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "account_locked": True,
                "account_label": _account_label(selected_account),
                "account_number": selected_account.account_number if selected_account else None,
                "selected_account_id": str(selected_account.id) if selected_account else str(payment.account_id) if payment else None,
                "currency_locked": bool(payment.invoice_id) if payment else False,
                "show_invoice_typeahead": False,
                "selected_invoice_id": str(payment.invoice_id) if payment and payment.invoice_id else None,
                "balance_value": balance_value,
                "balance_display": balance_display,
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/billing/payments", status_code=303)


@router.get("/payments/import", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def payment_import_page(request: Request, db: Session = Depends(get_db)):
    """Bulk payment import page."""
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/payment_import.html",
        {
            "request": request,
            "active_page": "payments",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/payments/import", dependencies=[Depends(require_permission("billing:write"))])
async def payment_import_submit(request: Request, db: Session = Depends(get_db)):
    """Process bulk payment import from JSON."""
    from fastapi.responses import JSONResponse
    from app.web.admin import get_current_user

    try:
        body = await request.json()
        payments_data = body.get("payments", [])

        if not payments_data:
            return JSONResponse({"message": "No payments to import"}, status_code=400)

        # Get default currency from settings
        default_currency = settings_spec.get_setting_value(
            db, SettingDomain.billing, "default_currency", default="NGN"
        )

        imported_count = 0
        errors = []

        for idx, row in enumerate(payments_data):
            try:
                # Resolve account by account_number or account_id
                account_id = None
                account_number = row.get("account_number")
                account_id_str = row.get("account_id")

                if account_number:
                    # Look up account by number
                    accounts = subscriber_service.accounts.list(
                        db=db,
                        subscriber_id=None,
                        reseller_id=None,
                        order_by="created_at",
                        order_dir="desc",
                        limit=10000,
                        offset=0,
                    )
                    for acc in accounts:
                        if acc.account_number == account_number:
                            account_id = acc.id
                            break
                    if not account_id:
                        errors.append(f"Row {idx + 1}: Account not found: {account_number}")
                        continue
                elif account_id_str:
                    try:
                        account_id = UUID(account_id_str)
                    except ValueError:
                        errors.append(f"Row {idx + 1}: Invalid account_id format")
                        continue
                else:
                    errors.append(f"Row {idx + 1}: Missing account identifier")
                    continue

                # Parse amount
                try:
                    amount = Decimal(str(row.get("amount", "0")))
                    if amount <= 0:
                        errors.append(f"Row {idx + 1}: Amount must be positive")
                        continue
                except (ValueError, InvalidOperation):
                    errors.append(f"Row {idx + 1}: Invalid amount")
                    continue

                # Parse date if provided
                paid_at = None
                date_str = row.get("date")
                if date_str:
                    try:
                        paid_at = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
                    except ValueError:
                        # Try alternate date formats
                        for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"]:
                            try:
                                paid_at = datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
                                break
                            except ValueError:
                                continue

                # Get currency and reference
                currency = row.get("currency", default_currency) or default_currency
                reference = row.get("reference", "")

                # Create payment
                payload = PaymentCreate(
                    account_id=account_id,
                    amount=amount,
                    currency=currency.upper(),
                    status="completed",
                    memo=f"Imported payment{': ' + reference if reference else ''}",
                )
                payment = billing_service.payments.create(db, payload)

                # Update paid_at if provided
                if paid_at:
                    payment.paid_at = paid_at
                    db.commit()

                imported_count += 1

            except Exception as exc:
                errors.append(f"Row {idx + 1}: {str(exc)}")
                continue

        # Log audit event
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="import",
            entity_type="payment",
            entity_id="bulk",
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"imported": imported_count, "errors": len(errors)},
        )

        return JSONResponse({
            "imported": imported_count,
            "errors": errors[:10] if errors else [],  # Return first 10 errors
            "total_errors": len(errors),
        })

    except Exception as exc:
        return JSONResponse({"message": f"Import failed: {str(exc)}"}, status_code=500)


@router.get("/payments/import/template", dependencies=[Depends(require_permission("billing:read"))])
def payment_import_template():
    """Download CSV template for payment import."""
    from fastapi.responses import Response

    csv_content = """account_number,amount,currency,reference,date
ACC-001,15000,NGN,TRF-001,2024-01-15
ACC-002,25000,NGN,TRF-002,2024-01-16
"""
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=payment_import_template.csv"},
    )


@router.get("/accounts", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def accounts_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    customer_ref: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """List all billing accounts."""
    offset = (page - 1) * per_page
    accounts = []
    total = 0
    if customer_ref:
        from app.models.subscriber import Subscriber, SubscriberAccount
        from sqlalchemy.orm import selectinload
        subscriber_ids = _subscriber_ids_for_customer(db, customer_ref)
        if subscriber_ids:
            query = (
                db.query(SubscriberAccount)
                .options(
                    selectinload(SubscriberAccount.subscriber).selectinload(
                        Subscriber.person
                    )
                )
                .filter(SubscriberAccount.subscriber_id.in_(subscriber_ids))
                .order_by(SubscriberAccount.created_at.desc())
            )
            total = query.count()
            accounts = query.offset(offset).limit(per_page).all()
    else:
        accounts = subscriber_service.accounts.list(
            db=db,
            subscriber_id=None,
            reseller_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=per_page,
            offset=offset,
        )
        all_accounts = subscriber_service.accounts.list(
            db=db,
            subscriber_id=None,
            reseller_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=10000,
            offset=0,
        )
        total = len(all_accounts)
    total_pages = (total + per_page - 1) // per_page

    # Get sidebar stats and current user
    from app.web.admin import get_sidebar_stats, get_current_user
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/billing/accounts.html",
        {
            "request": request,
            "accounts": accounts,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "customer_ref": customer_ref,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.get("/accounts/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def account_new(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user

    customer_ref = request.query_params.get("customer_ref")
    customer_label = _customer_label(db, customer_ref)
    resellers = subscriber_service.resellers.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    tax_rates = billing_service.tax_rates.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    return templates.TemplateResponse(
        "admin/billing/account_form.html",
        {
            "request": request,
            "action_url": "/admin/billing/accounts",
            "form_title": "New Billing Account",
            "submit_label": "Create Account",
            "active_page": "accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "resellers": resellers,
            "tax_rates": tax_rates,
            "customer_ref": customer_ref,
            "customer_label": customer_label,
        },
    )


@router.post("/accounts", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def account_create(
    request: Request,
    subscriber_id: str | None = Form(None),
    customer_ref: str | None = Form(None),
    reseller_id: str | None = Form(None),
    tax_rate_id: str | None = Form(None),
    account_number: str | None = Form(None),
    status: str | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        if not subscriber_id and customer_ref:
            subscribers = _subscribers_for_customer(db, customer_ref)
            if len(subscribers) == 1:
                subscriber_id = subscribers[0]["id"]
            elif len(subscribers) > 1:
                raise ValueError("Multiple subscribers found; please choose one.")
        if not subscriber_id:
            raise ValueError("subscriber_id is required")
        payload = SubscriberAccountCreate(
            subscriber_id=_parse_uuid(subscriber_id, "subscriber_id"),
            reseller_id=UUID(reseller_id) if reseller_id else None,
            tax_rate_id=UUID(tax_rate_id) if tax_rate_id else None,
            account_number=account_number.strip() if account_number else None,
            status=status or "active",
            notes=notes.strip() if notes else None,
        )
        account = subscriber_service.accounts.create(db, payload)
    except Exception as exc:
        from app.web.admin import get_sidebar_stats, get_current_user
        # Fetch lookup data for dropdowns on error
        resellers = subscriber_service.resellers.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=500,
            offset=0,
        )
        tax_rates = billing_service.tax_rates.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        return templates.TemplateResponse(
            "admin/billing/account_form.html",
            {
                "request": request,
                "action_url": "/admin/billing/accounts",
                "form_title": "New Billing Account",
                "submit_label": "Create Account",
                "error": str(exc),
                "active_page": "accounts",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "resellers": resellers,
                "tax_rates": tax_rates,
                "customer_ref": customer_ref,
                "customer_label": _customer_label(db, customer_ref),
                "selected_subscriber_id": subscriber_id,
            },
            status_code=400,
        )
    return RedirectResponse(url=f"/admin/billing/accounts/{account.id}", status_code=303)


@router.get("/accounts/{account_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def account_detail(request: Request, account_id: UUID, db: Session = Depends(get_db)):
    account = subscriber_service.accounts.get(db, str(account_id))
    invoices = billing_service.invoices.list(
        db=db,
        account_id=str(account_id),
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/account_detail.html",
        {
            "request": request,
            "account": account,
            "invoices": invoices,
            "active_page": "accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/tax-rates", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def billing_tax_rates(request: Request, db: Session = Depends(get_db)):
    rates = billing_service.tax_rates.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=200,
        offset=0,
    )
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/tax_rates.html",
        {
            "request": request,
            "rates": rates,
            "active_page": "tax-rates",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/tax-rates", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def billing_tax_rate_create(
    request: Request,
    name: str = Form(...),
    rate: str = Form(...),
    code: str | None = Form(None),
    description: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        payload = TaxRateCreate(
            name=name.strip(),
            rate=_parse_decimal(rate, "rate"),
            code=code.strip() if code else None,
            description=description.strip() if description else None,
        )
        billing_service.tax_rates.create(db, payload)
    except Exception as exc:
        rates = billing_service.tax_rates.list(
            db=db,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/billing/tax_rates.html",
            {
                "request": request,
                "rates": rates,
                "error": str(exc),
                "active_page": "tax-rates",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/billing/tax-rates", status_code=303)


@router.get("/ar-aging", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def billing_ar_aging(request: Request, db: Session = Depends(get_db)):
    invoices = billing_service.invoices.list(
        db=db,
        account_id=None,
        status=None,
        is_active=None,
        order_by="due_at",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    today = datetime.now(timezone.utc).date()
    buckets = {
        "current": [],
        "1_30": [],
        "31_60": [],
        "61_90": [],
        "90_plus": [],
    }
    for invoice in invoices:
        status = getattr(invoice, "status", None)
        status_val = status.value if hasattr(status, "value") else str(status or "")
        if status_val in {"paid", "void"}:
            continue
        due_at = invoice.due_at.date() if invoice.due_at else None
        if not due_at or due_at >= today:
            buckets["current"].append(invoice)
            continue
        days = (today - due_at).days
        if days <= 30:
            buckets["1_30"].append(invoice)
        elif days <= 60:
            buckets["31_60"].append(invoice)
        elif days <= 90:
            buckets["61_90"].append(invoice)
        else:
            buckets["90_plus"].append(invoice)
    totals = {
        key: sum(float(getattr(inv, "balance_due", 0) or 0) for inv in items)
        for key, items in buckets.items()
    }
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/ar_aging.html",
        {
            "request": request,
            "buckets": buckets,
            "totals": totals,
            "active_page": "ar-aging",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/dunning", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def billing_dunning(
    request: Request,
    page: int = 1,
    status: str | None = None,
    customer_ref: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from app.models.collections import DunningCase, DunningCaseStatus
    from app.web.admin import get_sidebar_stats, get_current_user

    per_page = 50
    offset = (page - 1) * per_page

    account_ids = []
    customer_filtered = bool(customer_ref)
    if customer_ref:
        account_ids = [UUID(item["id"]) for item in _accounts_for_customer(db, customer_ref)]

    # Get status counts for filter badges
    if customer_filtered and not account_ids:
        status_counts = {
            "open": 0,
            "paused": 0,
            "resolved": 0,
            "closed": 0,
        }
    else:
        status_query = db.query(DunningCase)
        if account_ids:
            status_query = status_query.filter(DunningCase.account_id.in_(account_ids))
        status_counts = {
            "open": status_query.filter(DunningCase.status == DunningCaseStatus.open).count(),
            "paused": status_query.filter(DunningCase.status == DunningCaseStatus.paused).count(),
            "resolved": status_query.filter(DunningCase.status == DunningCaseStatus.resolved).count(),
            "closed": status_query.filter(DunningCase.status == DunningCaseStatus.closed).count(),
        }

    # Get total count for pagination
    cases = []
    total = 0
    total_pages = 1
    if account_ids:
        count_query = db.query(DunningCase).filter(DunningCase.account_id.in_(account_ids))
        if status:
            count_query = count_query.filter(DunningCase.status == status)
        total = count_query.count()
        total_pages = (total + per_page - 1) // per_page if total > 0 else 1
        cases = (
            count_query.order_by(DunningCase.created_at.desc())
            .offset(offset)
            .limit(per_page)
            .all()
        )
    elif not customer_filtered:
        count_query = db.query(DunningCase)
        if status:
            count_query = count_query.filter(DunningCase.status == status)
        total = count_query.count()
        total_pages = (total + per_page - 1) // per_page if total > 0 else 1
        cases = collections_service.dunning_cases.list(
            db=db,
            account_id=None,
            status=status,
            order_by="created_at",
            order_dir="desc",
            limit=per_page,
            offset=offset,
        )
    return templates.TemplateResponse(
        "admin/billing/dunning.html",
        {
            "request": request,
            "cases": cases,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "status": status,
            "status_counts": status_counts,
            "customer_ref": customer_ref,
            "active_page": "dunning",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/dunning/{case_id}/pause", dependencies=[Depends(require_permission("billing:write"))])
def dunning_pause(request: Request, case_id: str, db: Session = Depends(get_db)):
    """Pause a dunning case."""
    from app.web.admin import get_current_user
    collections_service.dunning_cases.pause(db=db, case_id=case_id)
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="pause",
        entity_type="dunning_case",
        entity_id=case_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post("/dunning/{case_id}/resume", dependencies=[Depends(require_permission("billing:write"))])
def dunning_resume(request: Request, case_id: str, db: Session = Depends(get_db)):
    """Resume a paused dunning case."""
    from app.web.admin import get_current_user
    collections_service.dunning_cases.resume(db=db, case_id=case_id)
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="resume",
        entity_type="dunning_case",
        entity_id=case_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post("/dunning/{case_id}/close", dependencies=[Depends(require_permission("billing:write"))])
def dunning_close(request: Request, case_id: str, db: Session = Depends(get_db)):
    """Close a dunning case."""
    from app.web.admin import get_current_user
    collections_service.dunning_cases.close(db=db, case_id=case_id)
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="close",
        entity_type="dunning_case",
        entity_id=case_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post("/dunning/bulk/pause", dependencies=[Depends(require_permission("billing:write"))])
def dunning_bulk_pause(request: Request, case_ids: str = Form(...), db: Session = Depends(get_db)):
    """Pause multiple dunning cases."""
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    count = 0
    for case_id in case_ids.split(","):
        case_id = case_id.strip()
        if case_id:
            try:
                collections_service.dunning_cases.pause(db=db, case_id=case_id)
                log_audit_event(
                    db=db,
                    request=request,
                    action="pause",
                    entity_type="dunning_case",
                    entity_id=case_id,
                    actor_id=str(current_user.get("subscriber_id")) if current_user else None,
                )
                count += 1
            except Exception:
                pass  # Skip errors for individual cases
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post("/dunning/bulk/resume", dependencies=[Depends(require_permission("billing:write"))])
def dunning_bulk_resume(request: Request, case_ids: str = Form(...), db: Session = Depends(get_db)):
    """Resume multiple paused dunning cases."""
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    count = 0
    for case_id in case_ids.split(","):
        case_id = case_id.strip()
        if case_id:
            try:
                collections_service.dunning_cases.resume(db=db, case_id=case_id)
                log_audit_event(
                    db=db,
                    request=request,
                    action="resume",
                    entity_type="dunning_case",
                    entity_id=case_id,
                    actor_id=str(current_user.get("subscriber_id")) if current_user else None,
                )
                count += 1
            except Exception:
                pass  # Skip errors for individual cases
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.get("/ledger", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def billing_ledger(
    request: Request,
    customer_ref: str | None = Query(None),
    db: Session = Depends(get_db),
):
    entry_type = request.query_params.get("entry_type")
    account_ids = []
    if customer_ref:
        account_ids = [UUID(item["id"]) for item in _accounts_for_customer(db, customer_ref)]
    entries = []
    if account_ids:
        query = db.query(LedgerEntry).filter(LedgerEntry.account_id.in_(account_ids))
        if entry_type:
            query = query.filter(
                LedgerEntry.entry_type == validate_enum(entry_type, LedgerEntryType, "entry_type")
            )
        query = query.filter(LedgerEntry.is_active.is_(True))
        entries = (
            query.order_by(LedgerEntry.created_at.desc()).limit(200).offset(0).all()
        )
    elif not customer_ref:
        entries = billing_service.ledger_entries.list(
            db=db,
            account_id=None,
            entry_type=entry_type,
            source=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/ledger.html",
        {
            "request": request,
            "entries": entries,
            "entry_type": entry_type,
            "customer_ref": customer_ref,
            "active_page": "ledger",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/collection-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
def collection_accounts_list(
    request: Request,
    show_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=False if show_inactive else None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/collection_accounts.html",
        {
            "request": request,
            "accounts": accounts,
            "account_types": [item.value for item in CollectionAccountType],
            "show_inactive": show_inactive,
            "active_page": "collection_accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/collection-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def collection_accounts_create(
    request: Request,
    name: str = Form(...),
    account_type: str = Form("bank"),
    currency: str = Form("NGN"),
    bank_name: str | None = Form(None),
    account_last4: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.create_collection_account(
            db=db,
            name=name,
            account_type=account_type,
            currency=currency,
            bank_name=bank_name,
            account_last4=account_last4,
            notes=notes,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/billing/collection-accounts", status_code=303)
    except Exception as exc:
        accounts = billing_service.collection_accounts.list(
            db=db,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=500,
            offset=0,
        )
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/billing/collection_accounts.html",
            {
                "request": request,
                "accounts": accounts,
                "account_types": [item.value for item in CollectionAccountType],
                "error": str(exc),
                "show_inactive": False,
                "active_page": "collection_accounts",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.get(
    "/collection-accounts/{account_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def collection_accounts_edit(request: Request, account_id: UUID, db: Session = Depends(get_db)):
    account = billing_service.collection_accounts.get(db, str(account_id))
    if not account:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Collection account not found"},
            status_code=404,
        )
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/collection_account_form.html",
        {
            "request": request,
            "account": account,
            "account_types": [item.value for item in CollectionAccountType],
            "action_url": f"/admin/billing/collection-accounts/{account_id}/edit",
            "form_title": "Edit Collection Account",
            "submit_label": "Update Account",
            "active_page": "collection_accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/collection-accounts/{account_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def collection_accounts_update(
    request: Request,
    account_id: UUID,
    name: str = Form(...),
    account_type: str = Form("bank"),
    currency: str = Form("NGN"),
    bank_name: str | None = Form(None),
    account_last4: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.update_collection_account(
            db=db,
            account_id=account_id,
            name=name,
            account_type=account_type,
            currency=currency,
            bank_name=bank_name,
            account_last4=account_last4,
            notes=notes,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/billing/collection-accounts", status_code=303)
    except Exception as exc:
        account = billing_service.collection_accounts.get(db, str(account_id))
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/billing/collection_account_form.html",
            {
                "request": request,
                "account": account,
                "account_types": [item.value for item in CollectionAccountType],
                "action_url": f"/admin/billing/collection-accounts/{account_id}/edit",
                "form_title": "Edit Collection Account",
                "submit_label": "Update Account",
                "error": str(exc),
                "active_page": "collection_accounts",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.post(
    "/collection-accounts/{account_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def collection_accounts_deactivate(account_id: UUID, db: Session = Depends(get_db)):
    billing_service.collection_accounts.delete(db, str(account_id))
    return RedirectResponse(url="/admin/billing/collection-accounts", status_code=303)


@router.post(
    "/collection-accounts/{account_id}/activate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def collection_accounts_activate(account_id: UUID, db: Session = Depends(get_db)):
    billing_service.collection_accounts.update(
        db,
        str(account_id),
        CollectionAccountUpdate(is_active=True),
    )
    return RedirectResponse(url="/admin/billing/collection-accounts", status_code=303)


@router.get(
    "/payment-channels",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
def payment_channels_list(request: Request, db: Session = Depends(get_db)):
    channels = billing_service.payment_channels.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    providers = billing_service.payment_providers.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    collection_accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/payment_channels.html",
        {
            "request": request,
            "channels": channels,
            "providers": providers,
            "collection_accounts": collection_accounts,
            "channel_types": [item.value for item in PaymentChannelType],
            "active_page": "payment_channels",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/payment-channels",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channels_create(
    request: Request,
    name: str = Form(...),
    channel_type: str = Form("other"),
    provider_id: str | None = Form(None),
    default_collection_account_id: str | None = Form(None),
    is_default: str | None = Form(None),
    is_active: str | None = Form(None),
    fee_rules: str | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.create_payment_channel(
            db=db,
            name=name,
            channel_type=channel_type,
            provider_id=provider_id,
            default_collection_account_id=default_collection_account_id,
            is_default=is_default,
            is_active=is_active,
            fee_rules=fee_rules,
            notes=notes,
        )
        return RedirectResponse(url="/admin/billing/payment-channels", status_code=303)
    except Exception as exc:
        channels = billing_service.payment_channels.list(
            db=db,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=500,
            offset=0,
        )
        providers = billing_service.payment_providers.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=200,
            offset=0,
        )
        collection_accounts = billing_service.collection_accounts.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=200,
            offset=0,
        )
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/billing/payment_channels.html",
            {
                "request": request,
                "channels": channels,
                "providers": providers,
                "collection_accounts": collection_accounts,
                "channel_types": [item.value for item in PaymentChannelType],
                "error": str(exc),
                "active_page": "payment_channels",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.get(
    "/payment-channels/{channel_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channels_edit(request: Request, channel_id: UUID, db: Session = Depends(get_db)):
    channel = billing_service.payment_channels.get(db, str(channel_id))
    if not channel:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment channel not found"},
            status_code=404,
        )
    providers = billing_service.payment_providers.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    collection_accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/payment_channel_form.html",
        {
            "request": request,
            "channel": channel,
            "providers": providers,
            "collection_accounts": collection_accounts,
            "channel_types": [item.value for item in PaymentChannelType],
            "action_url": f"/admin/billing/payment-channels/{channel_id}/edit",
            "form_title": "Edit Payment Channel",
            "submit_label": "Update Channel",
            "active_page": "payment_channels",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/payment-channels/{channel_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channels_update(
    request: Request,
    channel_id: UUID,
    name: str = Form(...),
    channel_type: str = Form("other"),
    provider_id: str | None = Form(None),
    default_collection_account_id: str | None = Form(None),
    is_default: str | None = Form(None),
    is_active: str | None = Form(None),
    fee_rules: str | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.update_payment_channel(
            db=db,
            channel_id=channel_id,
            name=name,
            channel_type=channel_type,
            provider_id=provider_id,
            default_collection_account_id=default_collection_account_id,
            is_default=is_default,
            is_active=is_active,
            fee_rules=fee_rules,
            notes=notes,
        )
        return RedirectResponse(url="/admin/billing/payment-channels", status_code=303)
    except Exception as exc:
        channel = billing_service.payment_channels.get(db, str(channel_id))
        providers = billing_service.payment_providers.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=200,
            offset=0,
        )
        collection_accounts = billing_service.collection_accounts.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=200,
            offset=0,
        )
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/billing/payment_channel_form.html",
            {
                "request": request,
                "channel": channel,
                "providers": providers,
                "collection_accounts": collection_accounts,
                "channel_types": [item.value for item in PaymentChannelType],
                "action_url": f"/admin/billing/payment-channels/{channel_id}/edit",
                "form_title": "Edit Payment Channel",
                "submit_label": "Update Channel",
                "error": str(exc),
                "active_page": "payment_channels",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.post(
    "/payment-channels/{channel_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channels_deactivate(channel_id: UUID, db: Session = Depends(get_db)):
    billing_service.payment_channels.delete(db, str(channel_id))
    return RedirectResponse(url="/admin/billing/payment-channels", status_code=303)


@router.get(
    "/payment-channel-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
def payment_channel_accounts_list(request: Request, db: Session = Depends(get_db)):
    mappings = billing_service.payment_channel_accounts.list(
        db=db,
        channel_id=None,
        collection_account_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    channels = billing_service.payment_channels.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    collection_accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/payment_channel_accounts.html",
        {
            "request": request,
            "mappings": mappings,
            "channels": channels,
            "collection_accounts": collection_accounts,
            "active_page": "payment_channel_accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/payment-channel-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channel_accounts_create(
    request: Request,
    channel_id: str = Form(...),
    collection_account_id: str = Form(...),
    currency: str | None = Form(None),
    priority: int = Form(0),
    is_default: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.create_payment_channel_account(
            db=db,
            channel_id=channel_id,
            collection_account_id=collection_account_id,
            currency=currency,
            priority=priority,
            is_default=is_default,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/billing/payment-channel-accounts", status_code=303)
    except Exception as exc:
        mappings = billing_service.payment_channel_accounts.list(
            db=db,
            channel_id=None,
            collection_account_id=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=500,
            offset=0,
        )
        channels = billing_service.payment_channels.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=200,
            offset=0,
        )
        collection_accounts = billing_service.collection_accounts.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=200,
            offset=0,
        )
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/billing/payment_channel_accounts.html",
            {
                "request": request,
                "mappings": mappings,
                "channels": channels,
                "collection_accounts": collection_accounts,
                "error": str(exc),
                "active_page": "payment_channel_accounts",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.get(
    "/payment-channel-accounts/{mapping_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channel_accounts_edit(request: Request, mapping_id: UUID, db: Session = Depends(get_db)):
    mapping = billing_service.payment_channel_accounts.get(db, str(mapping_id))
    if not mapping:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment channel mapping not found"},
            status_code=404,
        )
    channels = billing_service.payment_channels.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    collection_accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    from app.web.admin import get_sidebar_stats, get_current_user
    return templates.TemplateResponse(
        "admin/billing/payment_channel_account_form.html",
        {
            "request": request,
            "mapping": mapping,
            "channels": channels,
            "collection_accounts": collection_accounts,
            "action_url": f"/admin/billing/payment-channel-accounts/{mapping_id}/edit",
            "form_title": "Edit Channel Mapping",
            "submit_label": "Update Mapping",
            "active_page": "payment_channel_accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/payment-channel-accounts/{mapping_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channel_accounts_update(
    request: Request,
    mapping_id: UUID,
    channel_id: str = Form(...),
    collection_account_id: str = Form(...),
    currency: str | None = Form(None),
    priority: int = Form(0),
    is_default: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.update_payment_channel_account(
            db=db,
            mapping_id=mapping_id,
            channel_id=channel_id,
            collection_account_id=collection_account_id,
            currency=currency,
            priority=priority,
            is_default=is_default,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/billing/payment-channel-accounts", status_code=303)
    except Exception as exc:
        mapping = billing_service.payment_channel_accounts.get(db, str(mapping_id))
        channels = billing_service.payment_channels.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=200,
            offset=0,
        )
        collection_accounts = billing_service.collection_accounts.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=200,
            offset=0,
        )
        from app.web.admin import get_sidebar_stats, get_current_user
        return templates.TemplateResponse(
            "admin/billing/payment_channel_account_form.html",
            {
                "request": request,
                "mapping": mapping,
                "channels": channels,
                "collection_accounts": collection_accounts,
                "action_url": f"/admin/billing/payment-channel-accounts/{mapping_id}/edit",
                "form_title": "Edit Channel Mapping",
                "submit_label": "Update Mapping",
                "error": str(exc),
                "active_page": "payment_channel_accounts",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.post(
    "/payment-channel-accounts/{mapping_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channel_accounts_deactivate(mapping_id: UUID, db: Session = Depends(get_db)):
    billing_service.payment_channel_accounts.delete(db, str(mapping_id))
    return RedirectResponse(url="/admin/billing/payment-channel-accounts", status_code=303)
