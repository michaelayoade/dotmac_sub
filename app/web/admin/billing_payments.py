"""Admin billing payments routes."""

from decimal import Decimal, InvalidOperation
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.billing import Payment
from app.models.subscriber import Subscriber
from app.services import web_billing_customers as web_billing_customers_service
from app.services import web_billing_payment_forms as web_billing_payment_forms_service
from app.services import web_billing_payments as web_billing_payments_service
from app.services import web_billing_reconciliation as web_billing_reconciliation_service
from app.services.audit_helpers import build_audit_activities, log_audit_event
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_json_body

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


@router.get("/payments", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def payments_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    customer_ref: str | None = Query(None),
    partner_id: str | None = Query(None),
    status: str | None = Query(None),
    method: str | None = Query(None),
    search: str | None = Query(None),
    date_range: str | None = Query(None),
    unallocated_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    state = web_billing_payments_service.build_payments_list_data(
        db,
        page=page,
        per_page=per_page,
        customer_ref=customer_ref,
        partner_id=partner_id,
        status=status,
        method=method,
        search=search,
        date_range=date_range,
        unallocated_only=unallocated_only,
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payments.html",
        {
            "request": request,
            **state,
            "page_heading": "Unallocated Payments" if unallocated_only else "Payments",
            "page_subtitle": "Payments with no invoice allocations"
            if unallocated_only
            else "Track all payment transactions and collections",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/payments/export.csv", dependencies=[Depends(require_permission("billing:read"))])
def payments_export_csv(
    request: Request,
    customer_ref: str | None = Query(None),
    partner_id: str | None = Query(None),
    status: str | None = Query(None),
    method: str | None = Query(None),
    search: str | None = Query(None),
    date_range: str | None = Query(None),
    unallocated_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    state = web_billing_payments_service.build_payments_list_data(
        db,
        page=1,
        per_page=10000,
        customer_ref=customer_ref,
        partner_id=partner_id,
        status=status,
        method=method,
        search=search,
        date_range=date_range,
        unallocated_only=unallocated_only,
    )
    content = web_billing_payments_service.render_payments_csv(cast(list[Payment], state["payments"]))
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename=\"payments_export.csv\"'},
    )


@router.get("/payments/unallocated", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def payments_unallocated(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    customer_ref: str | None = Query(None),
    partner_id: str | None = Query(None),
    status: str | None = Query(None),
    method: str | None = Query(None),
    search: str | None = Query(None),
    date_range: str | None = Query(None),
    db: Session = Depends(get_db),
):
    return payments_list(
        request=request,
        page=page,
        per_page=per_page,
        customer_ref=customer_ref,
        partner_id=partner_id,
        status=status,
        method=method,
        search=search,
        date_range=date_range,
        unallocated_only=True,
        db=db,
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
    state = web_billing_payment_forms_service.build_new_form_state(
        db,
        invoice_id=invoice_id,
        invoice_alias=invoice,
        account_id=account_id,
        account_alias=account,
    )
    selected_account = cast(Subscriber | None, state["selected_account"])
    prefill = cast(dict[str, Any], state["prefill"])
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_form.html",
        {
            "request": request,
            "accounts": None,
            "payment_methods": [],
            "payment_method_types": [],
            "collection_accounts": state["collection_accounts"],
            "invoices": state["invoices"],
            "prefill": prefill,
            "invoice_label": state["invoice_label"],
            "action_url": "/admin/billing/payments/create",
            "form_title": "Record Payment",
            "submit_label": "Record Payment",
            "active_page": "payments",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "account_locked": bool(selected_account),
            "account_label": web_billing_payment_forms_service.account_label(selected_account) if selected_account else None,
            "account_number": selected_account.account_number if selected_account else None,
            "selected_account_id": str(selected_account.id) if selected_account else None,
            "currency_locked": bool(prefill.get("invoice_id")),
            "show_invoice_typeahead": not bool(selected_account),
            "selected_invoice_id": prefill.get("invoice_id"),
            "balance_value": state["balance_value"],
            "balance_display": state["balance_display"],
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
    state = web_billing_payment_forms_service.load_invoice_options_state(
        db,
        account_id=account_id,
        invoice_id=invoice_id,
    )
    return templates.TemplateResponse(
        "admin/billing/_payment_invoice_select.html",
        {
            "request": request,
            "invoices": state["invoices"],
            "selected_invoice_id": invoice_id,
            "invoice_label": state["invoice_label"],
            "show_invoice_typeahead": not bool(state["selected_account"]),
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
    state = web_billing_payment_forms_service.load_invoice_currency_state(
        db,
        invoice_id=invoice_id,
        currency=currency,
    )
    return templates.TemplateResponse(
        "admin/billing/_payment_currency_field.html",
        {
            "request": request,
            "currency_value": state["currency_value"],
            "currency_locked": state["currency_locked"],
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
    state = web_billing_payment_forms_service.load_invoice_details_state(
        db,
        invoice_id=invoice_id,
        amount=amount,
    )
    return templates.TemplateResponse(
        "admin/billing/_payment_amount_field.html",
        {
            "request": request,
            "amount_value": state["amount_value"],
            "balance_value": state["balance_value"],
            "balance_display": state["balance_display"],
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
    accounts = web_billing_customers_service.accounts_for_customer(db, customer_ref)
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
    subscribers = web_billing_customers_service.subscribers_for_customer(db, customer_ref)
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
        result = web_billing_payments_service.process_payment_create(
            db,
            account_id=account_id,
            amount=amount,
            currency=currency,
            status=status,
            invoice_id=invoice_id,
            collection_account_id=collection_account_id,
            memo=memo,
        )
        payment = cast(Payment, result["payment"])
        resolved_invoice = result["resolved_invoice"]
        balance_value = result["balance_value"]
        balance_display = result["balance_display"]
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="payment",
            entity_id=str(payment.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=cast(dict[str, object], result.get("audit_metadata") or {}),
        )
    except Exception as exc:
        deps = cast(
            dict[str, object],
            web_billing_payment_forms_service.load_create_error_dependencies(
                db,
                account_id=account_id,
                resolved_invoice=resolved_invoice,
            ),
        )
        error_state = web_billing_payment_forms_service.build_create_error_context(
            error=str(exc),
            deps=deps,
            resolved_invoice=resolved_invoice,
            invoice_id=invoice_id,
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/billing/payment_form.html",
            {
                "request": request,
                **error_state,
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "balance_value": balance_value,
                "balance_display": balance_display,
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/billing/payments", status_code=303)


@router.get("/payments/{payment_id:uuid}", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def payment_detail(request: Request, payment_id: UUID, db: Session = Depends(get_db)) -> HTMLResponse:
    state = web_billing_payments_service.build_payment_detail_data(db, payment_id=str(payment_id))
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_detail.html",
        {
            "request": request,
            **state,
            "activities": build_audit_activities(
                db, "payment", str(payment_id), limit=10
            ),
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/payments/{payment_id:uuid}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def payment_edit(request: Request, payment_id: UUID, db: Session = Depends(get_db)) -> HTMLResponse:
    state = web_billing_payments_service.build_payment_edit_data(db, payment_id=str(payment_id))
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment not found"},
            status_code=404,
        )
    payment = cast(Payment, state["payment"])
    selected_account = cast(Subscriber | None, state["selected_account"])
    primary_invoice_id = state["primary_invoice_id"]
    deps = cast(dict[str, Any], state["deps"])
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_form.html",
        {
            "request": request,
            "accounts": None,
            "payment_methods": deps["payment_methods"],
            "payment_method_types": deps["payment_method_types"],
            "invoices": deps["invoices"],
            "payment": payment,
            "action_url": f"/admin/billing/payments/{payment_id}/edit",
            "form_title": "Edit Payment",
            "submit_label": "Save Changes",
            "active_page": "payments",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "account_locked": True,
            "account_label": web_billing_payment_forms_service.account_label(selected_account),
            "account_number": selected_account.account_number if selected_account else None,
            "selected_account_id": str(selected_account.id) if selected_account else str(payment.account_id),
            "currency_locked": bool(primary_invoice_id),
            "show_invoice_typeahead": False,
            "selected_invoice_id": primary_invoice_id,
            "balance_value": deps["balance_value"],
            "balance_display": deps["balance_display"],
        },
    )


@router.post("/payments/{payment_id:uuid}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
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
        result = web_billing_payments_service.process_payment_update(
            db,
            payment_id=str(payment_id),
            account_id=account_id,
            amount=amount,
            currency=currency,
            status=status,
            invoice_id=invoice_id,
            payment_method_id=payment_method_id,
            memo=memo,
        )
        metadata_payload = cast(dict[str, object] | None, result.get("audit_metadata"))
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
        edit_state = web_billing_payments_service.build_payment_edit_data(
            db, payment_id=str(payment_id)
        )
        payment = edit_state["payment"] if edit_state else None
        selected_account = edit_state["selected_account"] if edit_state else None
        deps = cast(dict[str, object], edit_state["deps"]) if edit_state else {}
        error_state = web_billing_payment_forms_service.build_edit_error_context(
            payment=payment,
            payment_id=payment_id,
            error=str(exc),
            deps=deps,
            selected_account=selected_account,
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/billing/payment_form.html",
            {
                "request": request,
                **error_state,
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/billing/payments", status_code=303)


@router.get("/payments/import", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def payment_import_page(
    request: Request,
    history_handler: str | None = Query(None),
    history_status: str | None = Query(None),
    history_date_range: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    import_history = web_billing_payments_service.list_payment_import_history_filtered(
        db,
        limit=100,
        handler=history_handler,
        status=history_status,
        date_range=history_date_range,
    )
    return templates.TemplateResponse(
        "admin/billing/payment_import.html",
        {
            "request": request,
            "active_page": "payments",
            "active_menu": "billing",
            "import_history": import_history,
            "history_handler": history_handler,
            "history_status": history_status,
            "history_date_range": history_date_range,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/payments/reconciliation", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def payment_reconciliation_page(
    request: Request,
    date_range: str | None = Query(None),
    handler: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_billing_reconciliation_service.build_reconciliation_data(
        db,
        date_range=date_range,
        handler=handler,
    )
    return templates.TemplateResponse(
        "admin/billing/payment_reconciliation.html",
        {
            "request": request,
            "active_page": "payments",
            "active_menu": "billing",
            **state,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/payments/import/history.csv", dependencies=[Depends(require_permission("billing:read"))])
def payment_import_history_csv(
    request: Request,
    history_handler: str | None = Query(None),
    history_status: str | None = Query(None),
    history_date_range: str | None = Query(None),
    db: Session = Depends(get_db),
):
    rows = web_billing_payments_service.list_payment_import_history_filtered(
        db,
        limit=1000,
        handler=history_handler,
        status=history_status,
        date_range=history_date_range,
    )
    content = web_billing_payments_service.render_payment_import_history_csv(rows)
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="payment_import_history.csv"'},
    )


@router.post("/payments/import", dependencies=[Depends(require_permission("billing:write"))])
def payment_import_submit(
    request: Request,
    body: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    try:
        payments_data = body.get("payments", [])
        handler = body.get("handler")

        if not payments_data:
            return JSONResponse({"message": "No payments to import"}, status_code=400)

        default_currency = web_billing_payments_service.resolve_default_currency(db)
        normalized_rows = web_billing_payments_service.normalize_import_rows(
            payments_data,
            handler,
        )
        payment_source = body.get("payment_source")
        payment_method_type = body.get("payment_method_type")
        file_name = body.get("file_name")
        pair_inactive_customers = bool(body.get("pair_inactive_customers", True))
        row_count = len(normalized_rows)
        total_amount = 0.0
        for row in normalized_rows:
            try:
                total_amount += float(Decimal(str(row.get("amount", 0) or 0)))
            except (TypeError, ValueError, InvalidOperation):
                continue

        imported_count, errors = web_billing_payments_service.import_payments(
            db,
            normalized_rows,
            default_currency,
            payment_source=payment_source,
            payment_method_type=payment_method_type,
            pair_inactive_customers=pair_inactive_customers,
        )

        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="import",
            entity_type="payment",
            entity_id="bulk",
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "imported": imported_count,
                "errors": len(errors),
                "payment_source": payment_source,
                "payment_method_type": payment_method_type,
                "file_name": file_name,
                "row_count": row_count,
                "total_amount": total_amount,
                "pair_inactive_customers": pair_inactive_customers,
                "handler": handler,
            },
        )

        return JSONResponse(
            web_billing_payments_service.build_import_result_payload(
                imported_count=imported_count,
                errors=errors,
            )
        )

    except Exception as exc:
        return JSONResponse({"message": f"Import failed: {str(exc)}"}, status_code=500)


@router.get("/payments/import/template", dependencies=[Depends(require_permission("billing:read"))])
def payment_import_template():
    return Response(
        content=web_billing_payments_service.import_template_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=payment_import_template.csv"},
    )
