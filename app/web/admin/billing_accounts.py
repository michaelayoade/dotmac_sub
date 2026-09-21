"""Admin billing accounts routes."""

from typing import cast
from urllib.parse import quote_plus
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.catalog import BillingMode
from app.models.subscriber import Subscriber
from app.services import web_action_readiness
from app.services import web_billing_accounts as web_billing_accounts_service
from app.services import web_billing_statements as web_billing_statements_service
from app.services.audit_helpers import build_audit_activities
from app.services.auth_dependencies import has_permission, require_permission
from app.services.billing_mode_transitions import (
    BILLING_MODE_WRITE_SCOPE,
    ConfirmBillingModeTransitionCommand,
    PreviewBillingModeTransitionRequest,
    confirm_billing_mode_transition,
    preview_billing_mode_transition,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.file_storage import build_content_disposition
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


@router.get(
    "/accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:account:read"))],
)
def accounts_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    customer_ref: str | None = Query(None),
    reseller_id: str | None = Query(None),
    search: str | None = Query(None),
    status: str | None = Query(None),
    balance_filter: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_accounts_service.build_accounts_list_data(
        db,
        page=page,
        per_page=per_page,
        customer_ref=customer_ref,
        reseller_id=reseller_id,
        search=search,
        status=status,
        balance_filter=balance_filter,
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


@router.get(
    "/accounts/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:account:write"))],
)
def account_new(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/account_form.html",
        {
            "request": request,
            **web_billing_accounts_service.build_new_account_form_context(
                db,
                customer_ref=request.query_params.get("customer_ref"),
            ),
            "active_page": "accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:account:write"))],
)
def account_create(
    request: Request,
    subscriber_id: str | None = Form(None),
    customer_ref: str | None = Form(None),
    reseller_id: str | None = Form(None),
    tax_rate_id: str | None = Form(None),
    account_number: str | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        account, selected_subscriber_id = (
            web_billing_accounts_service.create_account_from_form_web(
                db,
                request=request,
                actor_id=_actor_id(request),
                subscriber_id=subscriber_id,
                customer_ref=customer_ref,
                reseller_id=reseller_id,
                tax_rate_id=tax_rate_id,
                account_number=account_number,
                notes=notes,
            )
        )
    except Exception as exc:
        db.rollback()
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/billing/account_form.html",
            {
                "request": request,
                **web_billing_accounts_service.build_new_account_form_context(
                    db,
                    customer_ref=customer_ref,
                    selected_subscriber_id=selected_subscriber_id
                    if "selected_subscriber_id" in locals()
                    else subscriber_id,
                    error=str(exc),
                ),
                "active_page": "accounts",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(
        url=f"/admin/billing/accounts/{account.id}", status_code=303
    )


@router.get(
    "/accounts/{account_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:account:write"))],
)
def account_edit(request: Request, account_id: UUID, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/account_form.html",
        {
            "request": request,
            **web_billing_accounts_service.build_edit_account_form_context(
                db,
                account_id=str(account_id),
            ),
            "active_page": "accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/accounts/{account_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:account:write"))],
)
def account_update(
    request: Request,
    account_id: UUID,
    reseller_id: str | None = Form(None),
    tax_rate_id: str | None = Form(None),
    account_number: str | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    try:
        account = web_billing_accounts_service.update_account_from_form_web(
            db,
            request=request,
            actor_id=_actor_id(request),
            account_id=str(account_id),
            reseller_id=reseller_id,
            tax_rate_id=tax_rate_id,
            account_number=account_number,
            notes=notes,
        )
        return RedirectResponse(
            url=f"/admin/billing/accounts/{account.id}", status_code=303
        )
    except Exception as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/billing/account_form.html",
            {
                "request": request,
                **web_billing_accounts_service.build_edit_account_form_context(
                    db,
                    account_id=str(account_id),
                    reseller_id=reseller_id,
                    tax_rate_id=tax_rate_id,
                    error=str(exc),
                ),
                "active_page": "accounts",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.get(
    "/accounts/{account_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:account:read"))],
)
def account_detail(
    request: Request,
    account_id: UUID,
    statement_start: str | None = Query(None),
    statement_end: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_accounts_service.build_account_detail_data(
        db, account_id=str(account_id)
    )
    auth = getattr(getattr(request, "state", None), "auth", None) or {}
    billing_mode_transition = None
    billing_mode_transition_panel = None
    if has_permission(auth, db, BILLING_MODE_WRITE_SCOPE):
        account = cast(Subscriber, state["account"])
        target_mode = (
            BillingMode.postpaid
            if account.billing_mode == BillingMode.prepaid
            else BillingMode.prepaid
        )
        billing_mode_transition = preview_billing_mode_transition(
            db,
            PreviewBillingModeTransitionRequest(
                account_id=account_id,
                target_mode=target_mode,
            ),
        )
        billing_mode_transition_panel = web_action_readiness.readiness_panel(
            billing_mode_transition.readiness,
            audience="staff",
        )
    statement_range = web_billing_statements_service.parse_statement_range(
        statement_start, statement_end
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
            "billing_mode_transition": billing_mode_transition,
            "billing_mode_transition_panel": billing_mode_transition_panel,
            "billing_mode_idempotency_key": f"billing-mode:{uuid4()}",
            "billing_mode_message": request.query_params.get("billing_mode_message"),
            "billing_mode_error": request.query_params.get("billing_mode_error"),
        },
    )


@router.post(
    "/accounts/{account_id}/billing-mode",
    dependencies=[Depends(require_permission(BILLING_MODE_WRITE_SCOPE))],
)
def account_billing_mode_transition(
    request: Request,
    account_id: UUID,
    target_mode: str = Form(...),
    preview_fingerprint: str = Form(..., min_length=64, max_length=64),
    idempotency_key: str = Form(..., min_length=16, max_length=120),
    reason: str = Form(..., min_length=8, max_length=500),
    db: Session = Depends(get_db),
):
    try:
        target = BillingMode(target_mode.strip().lower())
    except ValueError:
        return RedirectResponse(
            url=(
                f"/admin/billing/accounts/{account_id}?billing_mode_error="
                f"{quote_plus('Select a supported billing mode.')}"
            ),
            status_code=303,
        )
    actor_id = _actor_id(request) or "admin"
    command_id = uuid4()
    db_session_adapter.release_read_transaction(db)
    try:
        outcome = confirm_billing_mode_transition(
            db,
            ConfirmBillingModeTransitionCommand(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=f"user:{actor_id}",
                    scope=BILLING_MODE_WRITE_SCOPE,
                    reason=reason,
                    idempotency_key=idempotency_key,
                ),
                account_id=account_id,
                target_mode=target,
                expected_preview_fingerprint=preview_fingerprint,
            ),
        )
    except DomainError as exc:
        return RedirectResponse(
            url=(
                f"/admin/billing/accounts/{account_id}?billing_mode_error="
                f"{quote_plus(exc.message)}"
            ),
            status_code=303,
        )
    message = (
        f"Billing mode changed from {outcome.prior_mode.value} "
        f"to {outcome.billing_mode.value}."
    )
    return RedirectResponse(
        url=(
            f"/admin/billing/accounts/{account_id}?billing_mode_message="
            f"{quote_plus(message)}"
        ),
        status_code=303,
    )


@router.get(
    "/accounts/{account_id}/statement",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:account:read"))],
)
def account_statement_fragment(
    request: Request,
    account_id: UUID,
    statement_start: str | None = Query(None),
    statement_end: str | None = Query(None),
    db: Session = Depends(get_db),
):
    statement_range = web_billing_statements_service.parse_statement_range(
        statement_start, statement_end
    )
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(
            url=(
                f"/admin/billing/accounts/{account_id}"
                f"?statement_start={statement_range.start_date.isoformat()}"
                f"&statement_end={statement_range.end_date.isoformat()}"
            ),
            status_code=303,
        )
    account = web_billing_accounts_service.get_account_detail_identity(
        db, account_id=str(account_id)
    )
    statement = web_billing_statements_service.build_account_statement(
        db,
        account_id=account_id,
        date_range=statement_range,
    )
    return templates.TemplateResponse(
        "admin/billing/_account_statement.html",
        {
            "request": request,
            "account_id": account_id,
            "recipient_email": account.email,
            "statement_range": statement_range,
            "statement": statement,
        },
    )


@router.get(
    "/accounts/{account_id}/statement.csv",
    dependencies=[Depends(require_permission("billing:account:read"))],
)
def account_statement_csv(
    account_id: UUID,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_accounts_service.build_account_detail_data(
        db, account_id=str(account_id)
    )
    account = state["account"]
    date_range = web_billing_statements_service.parse_statement_range(
        start_date, end_date
    )
    statement = web_billing_statements_service.build_account_statement(
        db,
        account_id=account_id,
        date_range=date_range,
    )
    content, filename = web_billing_statements_service.render_account_statement_csv(
        account=account,
        account_id=account_id,
        date_range=date_range,
        statement=statement,
    )
    headers = {"Content-Disposition": build_content_disposition(filename)}
    return StreamingResponse(iter([content]), media_type="text/csv", headers=headers)


@router.post(
    "/accounts/{account_id}/statement/send",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:account:write"))],
)
def account_statement_send(
    request: Request,
    account_id: UUID,
    start_date: str | None = Form(None),
    end_date: str | None = Form(None),
    recipient_email: str | None = Form(None),
    db: Session = Depends(get_db),
):
    state = web_billing_accounts_service.build_account_detail_data(
        db, account_id=str(account_id)
    )
    account = state["account"]
    date_range = web_billing_statements_service.build_and_queue_account_statement_email(
        db,
        account=account,
        account_id=account_id,
        start_date=start_date,
        end_date=end_date,
        recipient_email=recipient_email,
    )
    return RedirectResponse(
        url=f"/admin/billing/accounts/{account_id}?statement_start={date_range.start_date.isoformat()}&statement_end={date_range.end_date.isoformat()}",
        status_code=303,
    )
