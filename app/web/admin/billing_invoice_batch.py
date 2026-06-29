"""Admin billing invoice batch routes."""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_billing_invoice_batch as web_billing_invoice_batch_service
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


@router.post(
    "/invoices/generate-batch",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:batch:write"))],
)
def invoice_generate_batch(
    request: Request,
    billing_cycle: str | None = Form(None),
    billing_date: str | None = Form(None),
    db: Session = Depends(get_db),
):
    note = web_billing_invoice_batch_service.run_batch_with_date(
        db,
        billing_cycle=billing_cycle,
        billing_date=billing_date,
    )
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"{note}"
        "</div>"
    )


@router.post(
    "/invoices/batch/{run_id}/retry",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:batch:write"))],
)
def invoice_batch_retry(
    request: Request,
    run_id: str,
    db: Session = Depends(get_db),
):
    note = web_billing_invoice_batch_service.retry_batch_run(
        db,
        run_id=run_id,
    )
    query = urlencode({"note": note})
    return RedirectResponse(
        url=f"/admin/billing/invoices/batch?{query}", status_code=303
    )


@router.get(
    "/invoices/batch",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:batch:read"))],
)
def invoice_batch(
    request: Request,
    note: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_billing_invoice_batch_service.build_batch_page_state(db, note=note)
    return templates.TemplateResponse(
        "admin/billing/invoice_batch.html",
        {
            "request": request,
            "active_page": "invoices",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            **state,
        },
    )


@router.post(
    "/invoices/batch/schedule",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:batch:write"))],
)
def invoice_batch_schedule_update(
    request: Request,
    schedule_enabled: str | None = Form(None),
    run_day: str | None = Form(None),
    run_time: str | None = Form(None),
    timezone: str | None = Form(None),
    billing_cycle: str | None = Form(None),
    partner_ids: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    web_billing_invoice_batch_service.save_billing_run_schedule(
        db,
        enabled=bool(schedule_enabled),
        run_day=run_day,
        run_time=run_time,
        timezone=timezone,
        billing_cycle=billing_cycle,
        partner_ids=partner_ids,
    )
    return RedirectResponse(
        url="/admin/billing/invoices/batch?note=Billing+run+schedule+saved",
        status_code=303,
    )


@router.get(
    "/invoices/batch/history-panel",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:batch:read"))],
)
def invoice_batch_history_panel(
    request: Request,
    db: Session = Depends(get_db),
):
    recent_runs = web_billing_invoice_batch_service.list_recent_runs(db, limit=25)
    return templates.TemplateResponse(
        "admin/billing/_invoice_batch_history_table.html",
        {
            "request": request,
            "recent_runs": recent_runs,
        },
    )


@router.get(
    "/invoices/batch/history.csv",
    dependencies=[Depends(require_permission("billing:batch:read"))],
)
def invoice_batch_history_csv(
    request: Request,
    db: Session = Depends(get_db),
):
    rows = web_billing_invoice_batch_service.list_recent_runs(db, limit=1000)
    content = web_billing_invoice_batch_service.render_runs_csv(rows)
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="billing_run_history.csv"'
        },
    )


@router.get(
    "/invoices/batch/{run_id}/export.csv",
    dependencies=[Depends(require_permission("billing:batch:read"))],
)
def invoice_batch_run_csv(
    request: Request,
    run_id: str,
    db: Session = Depends(get_db),
):
    row = web_billing_invoice_batch_service.get_run_row(db, run_id=run_id)
    if not row:
        raise HTTPException(status_code=404, detail="Billing run not found")
    content = web_billing_invoice_batch_service.render_single_run_csv(row)
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="billing_run_{run_id}.csv"'
        },
    )


@router.post(
    "/invoices/generate-batch/preview",
    dependencies=[Depends(require_permission("billing:batch:read"))],
)
def invoice_generate_batch_preview(
    request: Request,
    billing_cycle: str | None = Form(None),
    subscription_status: str | None = Form(None),
    billing_date: str | None = Form(None),
    separate_by_partner: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        payload = web_billing_invoice_batch_service.preview_batch(
            db=db,
            billing_cycle=billing_cycle,
            billing_date=billing_date,
            separate_by_partner=bool(separate_by_partner),
        )
        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse(
            web_billing_invoice_batch_service.preview_error_payload(exc),
            status_code=400,
        )
