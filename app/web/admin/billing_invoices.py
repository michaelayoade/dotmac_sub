"""Admin billing management web routes."""

import secrets
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.http_query import OptionalDateQuery
from app.models.billing import Invoice, InvoiceStatus
from app.services import backdated_service_invoicing
from app.services import web_billing_customers as web_billing_customers_service
from app.services import web_billing_documents as web_billing_documents_service
from app.services import (
    web_billing_invoice_bulk_actions as web_billing_invoice_bulk_actions_service,
)
from app.services import web_billing_invoice_cache as web_billing_invoice_cache_service
from app.services import web_billing_invoice_forms as web_billing_invoice_forms_service
from app.services import web_billing_invoices as web_billing_invoices_service
from app.services import web_billing_overview as web_billing_overview_service
from app.services.auth_dependencies import has_permission, require_permission
from app.services.billing.invoices import is_controlled_invoice_draft
from app.services.inclusive_date_range import InclusiveDateRangeError
from app.services.list_query import ListQuery
from app.services.owner_commands import CommandContext

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


def _actor_id(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    if not current_user:
        return None
    value = current_user.get("actor_id") or current_user.get("subscriber_id")
    return str(value) if value else None


def _resolve_invoice_new_account_id(
    db: Session,
    *,
    account_id: str | None,
    account: str | None,
    customer_id: str | None,
    customer_type: str | None,
) -> str | None:
    resolved_account_id = account_id or account
    if resolved_account_id or not customer_id:
        return resolved_account_id

    kind = (customer_type or "person").strip().lower() or "person"
    customer_ref = f"{kind}:{customer_id}"
    customer_accounts = web_billing_customers_service.accounts_for_customer(
        db, customer_ref
    )
    if len(customer_accounts) == 1:
        return str(customer_accounts[0]["id"])
    return None


def _backdated_action(
    value: str,
) -> backdated_service_invoicing.BackdatedServiceInvoiceAction:
    try:
        return backdated_service_invoicing.BackdatedServiceInvoiceAction(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Unknown backdated invoice action"
        ) from exc


def _backdated_permission(
    action: backdated_service_invoicing.BackdatedServiceInvoiceAction,
) -> str:
    if action is backdated_service_invoicing.BackdatedServiceInvoiceAction.create:
        return backdated_service_invoicing.CREATE_PERMISSION
    return backdated_service_invoicing.RECONSTRUCT_PERMISSION


def _require_backdated_permission(
    auth: dict,
    db: Session,
    action: backdated_service_invoicing.BackdatedServiceInvoiceAction,
) -> str:
    permission = _backdated_permission(action)
    if not has_permission(auth, db, permission):
        raise HTTPException(status_code=403, detail=f"Missing permission: {permission}")
    return permission


def _backdated_actor(auth: dict) -> str:
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    principal_type = str(auth.get("principal_type") or "user").strip()
    return f"{principal_type}:{principal_id}"


def _backdated_error_status(
    error: backdated_service_invoicing.BackdatedServiceInvoiceError,
) -> int:
    if error.code.endswith((".account_not_found", ".subscription_not_found")):
        return 404
    if error.code.endswith(
        (
            ".stale_preview",
            ".idempotency_conflict",
            ".not_actionable",
            ".historical_terms_ambiguous",
            ".historical_terms_unproven",
        )
    ):
        return 409
    if error.code.endswith(".permission_denied"):
        return 403
    return 400


def _backdated_query(
    *,
    account_id: str,
    subscription_id: str,
    starts_on: str,
    ends_on: str,
    action: str,
    invoice_id: str | None,
) -> backdated_service_invoicing.BackdatedServiceInvoiceQuery:
    try:
        return backdated_service_invoicing.BackdatedServiceInvoiceQuery(
            account_id=UUID(account_id),
            subscription_id=UUID(subscription_id),
            starts_on=date.fromisoformat(starts_on),
            ends_on=date.fromisoformat(ends_on),
            action=_backdated_action(action),
            invoice_id=UUID(invoice_id) if invoice_id else None,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail="Account, subscription, invoice, and service dates must be valid.",
        ) from exc


def _render_backdated_service_invoice(
    request: Request,
    *,
    db: Session,
    auth: dict,
    account_id: UUID,
    selected_subscription_id: UUID | None = None,
    action: backdated_service_invoicing.BackdatedServiceInvoiceAction = (
        backdated_service_invoicing.BackdatedServiceInvoiceAction.create
    ),
    invoice_id: UUID | None = None,
    starts_on: date | None = None,
    ends_on: date | None = None,
    preview: backdated_service_invoicing.BackdatedServiceInvoicePreview | None = None,
    error: str | None = None,
    status_code: int = 200,
):
    from app.web.admin import get_current_user, get_sidebar_stats

    options = backdated_service_invoicing.list_form_options(db, account_id=account_id)
    return templates.TemplateResponse(
        "admin/billing/backdated_service_invoice.html",
        {
            "request": request,
            "options": options,
            "selected_subscription_id": selected_subscription_id,
            "selected_action": action.value,
            "selected_invoice_id": invoice_id,
            "selected_starts_on": starts_on,
            "selected_ends_on": ends_on,
            "preview": preview,
            "error": error,
            "idempotency_key": secrets.token_urlsafe(24),
            "can_create_backdated": has_permission(
                auth, db, backdated_service_invoicing.CREATE_PERMISSION
            ),
            "can_reconstruct_backdated": has_permission(
                auth, db, backdated_service_invoicing.RECONSTRUCT_PERMISSION
            ),
            "active_page": "invoices",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
        status_code=status_code,
    )


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def billing_overview(
    request: Request,
    partner_id: str | None = Query(None),
    location: str | None = Query(None),
    period: str = Query("this_month"),
    db: Session = Depends(get_db),
):
    """Billing overview page."""
    state = web_billing_overview_service.build_overview_data(
        db,
        partner_id=partner_id,
        location=location,
        period=period,
    )

    # Get sidebar stats and current user
    from app.web.admin import get_current_user, get_sidebar_stats

    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/billing/index.html",
        {
            "request": request,
            **state,
            "active_page": "billing",
            "active_menu": "billing",
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.get(
    "/cache",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def billing_invoice_cache_page(
    request: Request,
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    account_id: str | None = Query(None),
    notice: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_billing_invoice_cache_service.build_cache_page_state(
        db,
        date_from=date_from,
        date_to=date_to,
        account_id=account_id,
    )
    return templates.TemplateResponse(
        "admin/billing/cache.html",
        {
            "request": request,
            "active_page": "billing-cache",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "notice": notice,
            **state,
        },
    )


@router.post(
    "/cache/clear", dependencies=[Depends(require_permission("billing:invoice:update"))]
)
def billing_invoice_cache_clear(
    request: Request,
    mode: str = Form("all"),
    date_from: str | None = Form(None),
    date_to: str | None = Form(None),
    account_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    result = web_billing_invoice_cache_service.clear_cache_from_form(
        db,
        mode=mode,
        date_from=date_from,
        date_to=date_to,
        account_id=account_id,
    )
    notice = f"Cleared {result['invalidated']} cached invoice PDF(s)"
    return RedirectResponse(
        url=f"/admin/billing/cache?notice={notice}", status_code=303
    )


@router.get(
    "/invoices",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def invoices_list(
    request: Request,
    account_id: str | None = None,
    partner_id: str | None = Query(None),
    status: str | None = None,
    proforma_only: bool = Query(False),
    customer_ref: str | None = Query(None),
    search: str | None = Query(None),
    start_date: OptionalDateQuery = None,
    end_date: OptionalDateQuery = None,
    sort: Literal[
        "created_at", "invoice_number", "status", "total", "issued_at", "due_at"
    ] = Query("created_at"),
    direction: Literal["asc", "desc"] = Query("desc", alias="dir"),
    page: int = Query(1, ge=1),
    per_page: str | None = Query("25"),
    db: Session = Depends(get_db),
):
    """List all invoices with filtering."""
    try:
        list_query = web_billing_overview_service.build_invoice_list_query(
            account_id=account_id,
            partner_id=partner_id,
            status=status,
            proforma_only=proforma_only,
            customer_ref=customer_ref,
            search=search,
            start_date=start_date,
            end_date=end_date,
            sort_by=sort,
            sort_dir=direction,
            page=page,
            per_page=per_page,
        )
    except (InclusiveDateRangeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    state = web_billing_overview_service.build_invoices_list_data(
        db,
        list_query=list_query,
    )
    effective_query = state["list_query"]
    assert isinstance(effective_query, ListQuery)
    invoices = state["invoices"]
    assert isinstance(invoices, list)
    invoice_bulk_action_contract = (
        web_billing_invoice_bulk_actions_service.build_invoice_bulk_action_contract(
            db,
            auth=getattr(request.state, "auth", None) or {},
            invoices=invoices,
        )
    )
    page_was_clamped = effective_query.page != page

    if request.headers.get("HX-Request"):
        response = templates.TemplateResponse(
            "admin/billing/_invoices_list.html",
            {
                "request": request,
                **state,
                "invoice_bulk_action_contract": invoice_bulk_action_contract,
            },
        )
        if page_was_clamped:
            response.headers["HX-Replace-Url"] = effective_query.url(
                "/admin/billing/invoices"
            )
        return response

    if page_was_clamped:
        return RedirectResponse(
            url=effective_query.url("/admin/billing/invoices"),
            status_code=307,
        )

    # Get sidebar stats and current user
    from app.web.admin import get_current_user, get_sidebar_stats

    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/billing/invoices.html",
        {
            "request": request,
            **state,
            "invoice_bulk_action_contract": invoice_bulk_action_contract,
            "active_page": "invoices",
            "active_menu": "billing",
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.get(
    "/invoices/export.csv",
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def invoices_export_csv(
    request: Request,
    account_id: str | None = Query(None),
    partner_id: str | None = Query(None),
    status: str | None = Query(None),
    proforma_only: bool = Query(False),
    customer_ref: str | None = Query(None),
    search: str | None = Query(None),
    start_date: OptionalDateQuery = None,
    end_date: OptionalDateQuery = None,
    sort: Literal[
        "created_at", "invoice_number", "status", "total", "issued_at", "due_at"
    ] = Query("created_at"),
    direction: Literal["asc", "desc"] = Query("desc", alias="dir"),
    db: Session = Depends(get_db),
):
    try:
        list_query = web_billing_overview_service.build_invoice_list_query(
            account_id=account_id,
            partner_id=partner_id,
            status=status,
            proforma_only=proforma_only,
            customer_ref=customer_ref,
            search=search,
            start_date=start_date,
            end_date=end_date,
            sort_by=sort,
            sort_dir=direction,
        )
    except (InclusiveDateRangeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    rows = web_billing_overview_service.stream_invoices_csv(db, list_query=list_query)
    return StreamingResponse(
        rows,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="invoices_export.csv"'},
    )


@router.get(
    "/invoice-discounts",
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def invoice_discounts_report_redirect(request: Request) -> RedirectResponse:
    suffix = f"&{request.url.query}" if request.url.query else ""
    return RedirectResponse(
        url=f"/admin/reports/discounts?tab=invoices{suffix}", status_code=307
    )


@router.get(
    "/invoices/backdated-service",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def backdated_service_invoice_page(
    request: Request,
    account_id: UUID = Query(...),
    subscription_id: UUID | None = Query(None),
    action: str = Query("create"),
    invoice_id: UUID | None = Query(None),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:invoice:read")),
):
    selected_action = _backdated_action(action)
    _require_backdated_permission(auth, db, selected_action)
    try:
        return _render_backdated_service_invoice(
            request,
            db=db,
            auth=auth,
            account_id=account_id,
            selected_subscription_id=subscription_id,
            action=selected_action,
            invoice_id=invoice_id,
        )
    except backdated_service_invoicing.BackdatedServiceInvoiceError as exc:
        raise HTTPException(
            status_code=_backdated_error_status(exc), detail=exc.message
        ) from exc


@router.post(
    "/invoices/backdated-service/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def preview_backdated_service_invoice_page(
    request: Request,
    account_id: str = Form(...),
    subscription_id: str = Form(...),
    starts_on: str = Form(...),
    ends_on: str = Form(...),
    action: str = Form(...),
    invoice_id: str | None = Form(None),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:invoice:read")),
):
    query = _backdated_query(
        account_id=account_id,
        subscription_id=subscription_id,
        starts_on=starts_on,
        ends_on=ends_on,
        action=action,
        invoice_id=invoice_id,
    )
    _require_backdated_permission(auth, db, query.action)
    try:
        preview = backdated_service_invoicing.preview_backdated_service_invoice(
            db, query
        )
        return _render_backdated_service_invoice(
            request,
            db=db,
            auth=auth,
            account_id=query.account_id,
            selected_subscription_id=query.subscription_id,
            action=query.action,
            invoice_id=query.invoice_id,
            starts_on=query.starts_on,
            ends_on=query.ends_on,
            preview=preview,
        )
    except backdated_service_invoicing.BackdatedServiceInvoiceError as exc:
        if _backdated_error_status(exc) == 404:
            raise HTTPException(status_code=404, detail=exc.message) from exc
        return _render_backdated_service_invoice(
            request,
            db=db,
            auth=auth,
            account_id=query.account_id,
            selected_subscription_id=query.subscription_id,
            action=query.action,
            invoice_id=query.invoice_id,
            starts_on=query.starts_on,
            ends_on=query.ends_on,
            error=exc.message,
            status_code=400,
        )


@router.post(
    "/invoices/backdated-service/confirm",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def confirm_backdated_service_invoice_page(
    request: Request,
    account_id: str = Form(...),
    subscription_id: str = Form(...),
    starts_on: str = Form(...),
    ends_on: str = Form(...),
    action: str = Form(...),
    invoice_id: str | None = Form(None),
    preview_fingerprint: str = Form(...),
    idempotency_key: str = Form(...),
    reason: str = Form(...),
    confirmed: str | None = Form(None),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:invoice:read")),
):
    query = _backdated_query(
        account_id=account_id,
        subscription_id=subscription_id,
        starts_on=starts_on,
        ends_on=ends_on,
        action=action,
        invoice_id=invoice_id,
    )
    permission = _require_backdated_permission(auth, db, query.action)
    reason = reason.strip()
    if confirmed != "yes" or len(reason) < 12:
        raise HTTPException(
            status_code=422,
            detail="Confirm the reviewed action and provide a reason of at least 12 characters.",
        )
    context = CommandContext.system(
        actor=_backdated_actor(auth),
        scope=permission,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    # Permission and form reads opened a caller transaction.  The command owner
    # requires a clean session and rechecks every mutable preview fact itself.
    db.rollback()
    try:
        result = backdated_service_invoicing.execute_backdated_service_invoice(
            db,
            backdated_service_invoicing.CreateBackdatedServiceInvoiceCommand(
                context=context,
                query=query,
                expected_fingerprint=preview_fingerprint,
                permission_granted=True,
            ),
        )
    except backdated_service_invoicing.BackdatedServiceInvoiceError as exc:
        status_code = _backdated_error_status(exc)
        if status_code == 404:
            raise HTTPException(status_code=404, detail=exc.message) from exc
        return _render_backdated_service_invoice(
            request,
            db=db,
            auth=auth,
            account_id=query.account_id,
            selected_subscription_id=query.subscription_id,
            action=query.action,
            invoice_id=query.invoice_id,
            starts_on=query.starts_on,
            ends_on=query.ends_on,
            error=exc.message,
            status_code=status_code,
        )
    notice = (
        "Backdated service invoice draft created"
        if result.action
        is backdated_service_invoicing.BackdatedServiceInvoiceAction.create
        else "Invoice linked to the reviewed subscription service period"
    )
    return RedirectResponse(
        url=f"/admin/billing/invoices/{result.invoice_id}?notice={notice.replace(' ', '+')}",
        status_code=303,
    )


@router.get(
    "/invoices/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:create"))],
)
def invoice_new(
    request: Request,
    account_id: str | None = Query(None),
    account: str | None = Query(None),
    customer_id: str | None = Query(None),
    customer_type: str | None = Query(None),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:invoice:create")),
):
    resolved_account_id = _resolve_invoice_new_account_id(
        db,
        account_id=account_id,
        account=account,
        customer_id=customer_id,
        customer_type=customer_type,
    )
    state = web_billing_invoice_forms_service.new_form_state(
        db,
        account_id=resolved_account_id,
    )

    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/invoice_form.html",
        {
            "request": request,
            **state,
            "invoice": None,
            "action_url": "/admin/billing/invoices/create",
            "form_title": "New Invoice",
            "submit_label": "Create Invoice",
            "show_line_items": True,
            "active_page": "invoices",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "can_edit_issue_date": has_permission(auth, db, "billing:invoice:update"),
        },
    )


@router.post(
    "/invoices/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:create"))],
)
def invoice_create(
    request: Request,
    account_id: str | None = Form(None),
    customer_ref: str | None = Form(None),
    invoice_number: str | None = Form(None),
    status: str | None = Form(None),
    currency: str = Form("NGN"),
    issued_at: str | None = Form(None),
    due_at: str | None = Form(None),
    memo: str | None = Form(None),
    discount_type: str | None = Form(None),
    discount_value: str | None = Form(None),
    discount_reason: str | None = Form(None),
    proforma_invoice: str | None = Form(None),
    line_description: list[str] = Form([]),
    line_quantity: list[str] = Form([]),
    line_unit_price: list[str] = Form([]),
    line_tax_rate_id: list[str] = Form([]),
    line_items_json: str | None = Form(None),
    draft_idempotency_key: str | None = Form(None),
    issue_immediately: str | None = Form(None),
    send_notification: str | None = Form(None),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("billing:invoice:create")),
):
    today_iso = datetime.now(UTC).date().isoformat()
    if (
        issued_at
        and issued_at != today_iso
        and not has_permission(auth, db, "billing:invoice:update")
    ):
        raise HTTPException(
            status_code=403,
            detail="Finance permission is required to set a historical invoice date.",
        )
    try:
        invoice, resolved_account_id = web_billing_invoices_service.create_invoice_web(
            db,
            request=request,
            actor_id=_actor_id(request),
            account_id=account_id,
            customer_ref=customer_ref,
            invoice_number=invoice_number,
            status=status,
            currency=currency,
            issued_at=issued_at,
            due_at=due_at,
            memo=memo,
            discount_type=discount_type,
            discount_value=discount_value,
            discount_reason=discount_reason,
            proforma_invoice=proforma_invoice,
            line_description=line_description,
            line_quantity=line_quantity,
            line_unit_price=line_unit_price,
            line_tax_rate_id=line_tax_rate_id,
            line_items_json=line_items_json,
            draft_idempotency_key=draft_idempotency_key,
            issue_immediately=issue_immediately,
            send_notification=send_notification,
        )
    except Exception as exc:
        state = web_billing_invoice_forms_service.new_form_state(
            db,
            account_id=locals().get("resolved_account_id") or account_id,
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/billing/invoice_form.html",
            {
                "request": request,
                **state,
                "action_url": "/admin/billing/invoices/create",
                "form_title": "New Invoice",
                "submit_label": "Create Invoice",
                "error": str(exc),
                "active_page": "invoices",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "can_edit_issue_date": has_permission(
                    auth, db, "billing:invoice:update"
                ),
            },
            status_code=400,
        )
    return RedirectResponse(
        url=f"/admin/billing/invoices/{invoice.id}", status_code=303
    )


@router.post(
    "/invoices/generate-from-subscription",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:create"))],
)
def invoice_generate_from_subscription(
    request: Request,
    subscriber_id: str = Form(...),
    subscription_id: str = Form(...),
    db: Session = Depends(get_db),
):
    """Generate an invoice with line items auto-populated from a subscription's offer."""
    try:
        invoice = web_billing_invoices_service.generate_invoice_from_subscription_web(
            db,
            request=request,
            actor_id=_actor_id(request),
            subscriber_id=subscriber_id,
            subscription_id=subscription_id,
        )
    except Exception as exc:
        from app.web.admin import get_current_user, get_sidebar_stats

        list_query = web_billing_overview_service.build_invoice_list_query(
            account_id=None,
            partner_id=None,
            status=None,
            proforma_only=False,
            customer_ref=None,
            search=None,
            start_date=None,
            end_date=None,
            sort_by="created_at",
            sort_dir="desc",
            page=1,
            per_page="25",
        )
        state = web_billing_overview_service.build_invoices_list_data(
            db,
            list_query=list_query,
        )
        invoices = state["invoices"]
        assert isinstance(invoices, list)
        invoice_bulk_action_contract = (
            web_billing_invoice_bulk_actions_service.build_invoice_bulk_action_contract(
                db,
                auth=getattr(request.state, "auth", None) or {},
                invoices=invoices,
            )
        )
        status_code = exc.status_code if isinstance(exc, HTTPException) else 400
        error = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        return templates.TemplateResponse(
            "admin/billing/invoices.html",
            {
                "request": request,
                **state,
                "invoice_bulk_action_contract": invoice_bulk_action_contract,
                "error": error,
                "active_page": "invoices",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=status_code,
        )
    return RedirectResponse(
        url=f"/admin/billing/invoices/{invoice.id}", status_code=303
    )


@router.get(
    "/invoices/{invoice_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_edit(request: Request, invoice_id: UUID, db: Session = Depends(get_db)):
    state = web_billing_invoice_forms_service.edit_form_state(
        db,
        invoice_id=str(invoice_id),
    )
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )
    invoice = state["invoice"]
    if not isinstance(invoice, Invoice) or invoice.status != InvoiceStatus.draft:
        return templates.TemplateResponse(
            "admin/errors/409.html",
            {
                "request": request,
                "message": "Only draft invoices can be edited.",
            },
            status_code=409,
        )
    if is_controlled_invoice_draft(invoice):
        return templates.TemplateResponse(
            "admin/errors/409.html",
            {
                "request": request,
                "message": (
                    "This reviewed historical-service draft is sealed. Issue it "
                    "as reviewed, or void it and create a new preview."
                ),
            },
            status_code=409,
        )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/invoice_form.html",
        {
            "request": request,
            **state,
            "action_url": f"/admin/billing/invoices/{invoice_id}/edit",
            "form_title": "Edit Invoice",
            "submit_label": "Save Changes",
            "active_page": "invoices",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/invoices/{invoice_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_update(
    request: Request,
    invoice_id: UUID,
    account_id: str | None = Form(None),
    invoice_number: str | None = Form(None),
    status: str | None = Form(None),
    currency: str = Form("NGN"),
    issued_at: str | None = Form(None),
    due_at: str | None = Form(None),
    memo: str | None = Form(None),
    discount_type: str | None = Form(None),
    discount_value: str | None = Form(None),
    discount_reason: str | None = Form(None),
    proforma_invoice: str | None = Form(None),
    line_items_json: str | None = Form(None),
    draft_idempotency_key: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        web_billing_invoices_service.update_invoice_web(
            db,
            request=request,
            actor_id=_actor_id(request),
            invoice_id=str(invoice_id),
            account_id=account_id,
            invoice_number=invoice_number,
            status=status,
            currency=currency,
            issued_at=issued_at,
            due_at=due_at,
            memo=memo,
            discount_type=discount_type,
            discount_value=discount_value,
            discount_reason=discount_reason,
            proforma_invoice=proforma_invoice,
            line_items_json=line_items_json,
            draft_idempotency_key=draft_idempotency_key,
        )
    except Exception as exc:
        state = web_billing_invoice_forms_service.edit_form_state(
            db,
            invoice_id=str(invoice_id),
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/billing/invoice_form.html",
            {
                "request": request,
                **(state or {}),
                "action_url": f"/admin/billing/invoices/{invoice_id}/edit",
                "form_title": "Edit Invoice",
                "submit_label": "Save Changes",
                "show_line_items": False,
                "error": str(exc),
                "active_page": "invoices",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(
        url=f"/admin/billing/invoices/{invoice_id}", status_code=303
    )


@router.get(
    "/invoices/search",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def invoice_search(request: Request, db: Session = Depends(get_db)):
    return HTMLResponse("")


@router.get(
    "/invoices/filter",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def invoice_filter(request: Request, db: Session = Depends(get_db)):
    return HTMLResponse("")


@router.get(
    "/documents",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("billing:invoice:read")),
        Depends(require_permission("billing:payment:read")),
        Depends(require_permission("billing:credit_note:read")),
    ],
)
def billing_documents(
    request: Request,
    facet: str = Query(default="invoices"),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    context = {
        "request": request,
        "active_page": "billing",
        "active_menu": "billing",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }
    context.update(web_billing_documents_service.billing_documents_data(db, facet))
    return templates.TemplateResponse("admin/billing/documents.html", context)
