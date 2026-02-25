"""Admin billing accounts routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.notification import NotificationChannel, NotificationStatus
from app.services import notification as notification_service
from app.services import subscriber as subscriber_service
from app.services import web_billing_accounts as web_billing_accounts_service
from app.services import web_billing_statements as web_billing_statements_service
from app.services.audit_helpers import build_audit_activities, log_audit_event
from app.services.auth_dependencies import require_permission
from app.services.file_storage import build_content_disposition
from app.schemas.notification import NotificationCreate

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


@router.get("/accounts", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def accounts_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    customer_ref: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_accounts_service.build_accounts_list_data(
        db,
        page=page,
        per_page=per_page,
        customer_ref=customer_ref,
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/accounts.html",
        {
            "request": request,
            **state,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/accounts/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def account_new(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    customer_ref = request.query_params.get("customer_ref")
    form_data = web_billing_accounts_service.build_account_form_data(
        db,
        customer_ref=customer_ref,
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
            **form_data,
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
    from app.web.admin import get_current_user

    try:
        account, selected_subscriber_id, metadata_payload = (
            web_billing_accounts_service.create_account_from_form_with_metadata(
                db,
                subscriber_id=subscriber_id,
                customer_ref=customer_ref,
                reseller_id=reseller_id,
                tax_rate_id=tax_rate_id,
                account_number=account_number,
                status=status,
                notes=notes,
            )
        )
    except Exception as exc:
        from app.web.admin import get_current_user, get_sidebar_stats

        form_data = web_billing_accounts_service.build_account_form_data(
            db,
            customer_ref=customer_ref,
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
                **form_data,
                "selected_subscriber_id": selected_subscriber_id if "selected_subscriber_id" in locals() else subscriber_id,
            },
            status_code=400,
        )
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="subscriber_account",
        entity_id=str(account.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata_payload,
    )
    return RedirectResponse(url=f"/admin/billing/accounts/{account.id}", status_code=303)


@router.get("/accounts/{account_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def account_edit(request: Request, account_id: UUID, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    account = subscriber_service.accounts.get(db, str(account_id))
    customer_ref = (
        f"organization:{account.organization_id}" if account.organization_id else f"person:{account.id}"
    )
    form_data = web_billing_accounts_service.build_account_form_data(
        db,
        customer_ref=customer_ref,
    )
    return templates.TemplateResponse(
        "admin/billing/account_form.html",
        {
            "request": request,
            "action_url": f"/admin/billing/accounts/{account_id}/edit",
            "form_title": "Edit Billing Account",
            "submit_label": "Update Account",
            "active_page": "accounts",
            "active_menu": "billing",
            "account": account,
            "selected_subscriber_id": str(account.id),
            "selected_reseller_id": str(account.reseller_id) if account.reseller_id else "",
            "selected_tax_rate_id": str(account.tax_rate_id) if account.tax_rate_id else "",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            **form_data,
        },
    )


@router.post("/accounts/{account_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def account_update(
    request: Request,
    account_id: UUID,
    reseller_id: str | None = Form(None),
    tax_rate_id: str | None = Form(None),
    account_number: str | None = Form(None),
    status: str | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    before = subscriber_service.accounts.get(db, str(account_id))
    try:
        account, metadata_payload = web_billing_accounts_service.update_account_from_form_with_metadata(
            db,
            account_id=str(account_id),
            reseller_id=reseller_id,
            tax_rate_id=tax_rate_id,
            account_number=account_number,
            status=status,
            notes=notes,
        )
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="subscriber_account",
            entity_id=str(account_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
        return RedirectResponse(url=f"/admin/billing/accounts/{account.id}", status_code=303)
    except Exception as exc:
        customer_ref = (
            f"organization:{before.organization_id}" if before.organization_id else f"person:{before.id}"
        )
        form_data = web_billing_accounts_service.build_account_form_data(
            db,
            customer_ref=customer_ref,
        )
        return templates.TemplateResponse(
            "admin/billing/account_form.html",
            {
                "request": request,
                "action_url": f"/admin/billing/accounts/{account_id}/edit",
                "form_title": "Edit Billing Account",
                "submit_label": "Update Account",
                "error": str(exc),
                "active_page": "accounts",
                "active_menu": "billing",
                "account": before,
                "selected_subscriber_id": str(before.id),
                "selected_reseller_id": reseller_id or (str(before.reseller_id) if before.reseller_id else ""),
                "selected_tax_rate_id": tax_rate_id or (str(before.tax_rate_id) if before.tax_rate_id else ""),
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                **form_data,
            },
            status_code=400,
        )


@router.get("/accounts/{account_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def account_detail(
    request: Request,
    account_id: UUID,
    statement_start: str | None = Query(None),
    statement_end: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_accounts_service.build_account_detail_data(db, account_id=str(account_id))
    statement_range = web_billing_statements_service.parse_statement_range(statement_start, statement_end)
    statement = web_billing_statements_service.build_account_statement(
        db,
        account_id=account_id,
        date_range=statement_range,
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/account_detail.html",
        {
            "request": request,
            **state,
            "activities": build_audit_activities(
                db, "subscriber_account", str(account_id), limit=10
            ),
            "active_page": "accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "statement_range": statement_range,
            "statement": statement,
        },
    )


@router.get(
    "/accounts/{account_id}/statement.csv",
    dependencies=[Depends(require_permission("billing:read"))],
)
def account_statement_csv(
    account_id: UUID,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_accounts_service.build_account_detail_data(db, account_id=str(account_id))
    account = state["account"]
    date_range = web_billing_statements_service.parse_statement_range(start_date, end_date)
    statement = web_billing_statements_service.build_account_statement(
        db,
        account_id=account_id,
        date_range=date_range,
    )
    account_label = (
        account.account_number
        or (account.organization.name if getattr(account, "organization", None) else "")
        or f"Account {str(account.id)[:8]}"
    )
    content = web_billing_statements_service.render_statement_csv(
        account_label=account_label,
        account_id=account_id,
        date_range=date_range,
        statement=statement,
    )
    filename = f"statement_{account_label.replace(' ', '_')}_{date_range.start_date.isoformat()}_{date_range.end_date.isoformat()}.csv"
    headers = {"Content-Disposition": build_content_disposition(filename)}
    return StreamingResponse(iter([content]), media_type="text/csv", headers=headers)


@router.post(
    "/accounts/{account_id}/statement/send",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def account_statement_send(
    request: Request,
    account_id: UUID,
    start_date: str | None = Form(None),
    end_date: str | None = Form(None),
    recipient_email: str | None = Form(None),
    db: Session = Depends(get_db),
):
    state = web_billing_accounts_service.build_account_detail_data(db, account_id=str(account_id))
    account = state["account"]
    date_range = web_billing_statements_service.parse_statement_range(start_date, end_date)
    statement = web_billing_statements_service.build_account_statement(
        db,
        account_id=account_id,
        date_range=date_range,
    )
    to_email = (recipient_email or account.email or "").strip()
    if not to_email:
        raise HTTPException(status_code=400, detail="No recipient email set for this account")
    notification_service.notifications.create(
        db,
        NotificationCreate(
            channel=NotificationChannel.email,
            recipient=to_email,
            status=NotificationStatus.queued,
            subject=f"Account statement ({date_range.start_date.isoformat()} - {date_range.end_date.isoformat()})",
            body=(
                "Your account statement is ready.\n\n"
                f"Period: {date_range.start_date.isoformat()} to {date_range.end_date.isoformat()}\n"
                f"Opening balance: {statement['opening_balance']:.2f}\n"
                f"Closing balance: {statement['closing_balance']:.2f}\n"
                f"Transactions: {len(statement['rows'])}\n"
            ),
        ),
    )
    return RedirectResponse(
        url=f"/admin/billing/accounts/{account_id}?statement_start={date_range.start_date.isoformat()}&statement_end={date_range.end_date.isoformat()}",
        status_code=303,
    )
