"""Hidden, read-only Selfcare customer retention work queue."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import crm_reporting
from app.services.auth_dependencies import require_permission
from app.timezone import APP_TIMEZONE
from app.web.templates import templates

router = APIRouter(tags=["web-admin-customer-retention"])


def _period(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    if date_from is None and date_to is None:
        today = datetime.now(APP_TIMEZONE).date()
        return today - timedelta(days=29), today
    if date_from is None or date_to is None or date_from > date_to:
        raise HTTPException(status_code=422, detail="Enter a valid start and end date.")
    return date_from, date_to


def _base_context(request: Request, db: Session, active_page: str) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "active_page": active_page,
        "active_menu": "reports",
    }


@router.get(
    "/customer-retention",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def customer_retention_tracker(
    request: Request,
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    start, end = _period(date_from, date_to)
    context = _base_context(request, db, "customer-retention")
    context.update(
        {
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "metrics_url": "/admin/customer-retention/metrics?"
            + urlencode({"date_from": start, "date_to": end}),
            "queue_url": "/admin/customer-retention/queue?"
            + urlencode(
                {
                    "date_from": start,
                    "date_to": end,
                    "search": search or "",
                    "page": page,
                }
            ),
        }
    )
    return templates.TemplateResponse(
        "admin/reports/customer_retention_tracker.html", context
    )


@router.get(
    "/customer-retention/queue",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def customer_retention_queue(
    request: Request,
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    start, end = _period(date_from, date_to)
    report_page = crm_reporting.get_customer_retention_page(
        db=db,
        query=crm_reporting.CustomerRetentionPageQuery(search=search, page=page),
    )
    rows = report_page.rows
    segment_counts = (
        ("Suspended", report_page.suspended_count),
        ("Due Soon", report_page.due_soon_count),
    )
    total = report_page.tracked_count
    segment_breakdown = [
        {
            "segment": segment,
            "count": count,
            "share_pct": round((count / total) * 100, 1) if total else 0,
        }
        for segment, count in segment_counts
        if count
    ]
    context = _base_context(request, db, "customer-retention")
    context.update(
        {
            "rows": rows,
            "search": search or "",
            "tracked_count": report_page.tracked_count,
            "revenue_at_risk": report_page.revenue_at_risk,
            "segment_breakdown": segment_breakdown,
            "page": report_page.page,
            "per_page": report_page.per_page,
            "total_pages": report_page.total_pages,
            "total_count": report_page.total_count,
            "has_previous": report_page.has_previous,
            "has_next": report_page.has_next,
            "crm_state_unavailable": True,
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
        }
    )
    return templates.TemplateResponse(
        "admin/reports/_customer_retention_queue.html", context
    )


@router.get(
    "/customer-retention/metrics",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def customer_retention_metrics(
    request: Request,
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    start, end = _period(date_from, date_to)
    metrics = crm_reporting.get_customer_retention_metrics(
        db=db,
        query=crm_reporting.CustomerRetentionMetricsQuery(date_from=start, date_to=end),
    )
    return templates.TemplateResponse(
        "admin/reports/_customer_retention_metrics.html",
        {"request": request, "metrics": metrics},
    )


@router.get(
    "/customer-retention/export",
    dependencies=[Depends(require_permission("reports:billing:export"))],
)
def customer_retention_export(
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Response:
    export = crm_reporting.build_customer_retention_export(
        db=db, query=crm_reporting.CustomerRetentionExportQuery(search=search)
    )
    return Response(
        export.content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{export.filename}"'},
    )


@router.get(
    "/customer-retention/{customer_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def customer_retention_profile(
    customer_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    report_page = crm_reporting.get_customer_retention_page(
        db=db,
        query=crm_reporting.CustomerRetentionPageQuery(search=customer_id),
    )
    customer = next(
        (row for row in report_page.rows if row.customer_id == customer_id), None
    )
    context = _base_context(request, db, "customer-retention")
    context.update(
        {
            "customer": customer,
            "customer_id": customer_id,
            "crm_state_unavailable": True,
        }
    )
    return templates.TemplateResponse(
        "admin/reports/customer_retention_profile.html", context
    )
