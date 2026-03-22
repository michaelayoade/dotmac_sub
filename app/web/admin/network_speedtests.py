"""Admin network speed test web routes."""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_speedtests as web_network_speedtests_service
from app.services.audit_helpers import log_audit_event
from app.services.auth_dependencies import require_method_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/network",
    tags=["web-admin-network"],
    dependencies=[Depends(require_method_permission("network:speedtest:read", "network:speedtest:write"))],
)


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "network") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get("/speedtests", response_class=HTMLResponse)
def speedtests_list(
    request: Request,
    message: str | None = None,
    error_message: str | None = None,
    search: str | None = None,
    subscriber_id: str | None = None,
    network_device_id: str | None = None,
    pop_site_id: str | None = None,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        data = web_network_speedtests_service.list_page_data(
            db,
            search=search,
            subscriber_id=subscriber_id,
            network_device_id=network_device_id,
            pop_site_id=pop_site_id,
            source=source,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception as exc:
        # Reset failed transaction state before any fallback DB queries.
        db.rollback()
        fallback_error = str(exc)
        if "speed_test_results" in fallback_error and "does not exist" in fallback_error:
            fallback_error = (
                "Speed test tables are not available yet. "
                "Run database migrations, then reload this page."
            )
        try:
            reference_data = web_network_speedtests_service.speedtest_form_reference_data(db)
        except Exception:
            db.rollback()
            reference_data = {
                "subscribers": [],
                "subscriptions": [],
                "devices": [],
                "pop_sites": [],
                "sources": [],
            }
        data = {
            "results": [],
            "stats": {
                "total": 0,
                "avg_download": 0,
                "avg_upload": 0,
                "avg_latency": 0,
                "underperforming": 0,
            },
            "filters": {
                "search": str(search or "").strip(),
                "subscriber_id": str(subscriber_id or "").strip(),
                "network_device_id": str(network_device_id or "").strip(),
                "pop_site_id": str(pop_site_id or "").strip(),
                "source": str(source or "").strip(),
                "date_from": str(date_from or "").strip(),
                "date_to": str(date_to or "").strip(),
            },
            "invalid_filter_error": None,
            **reference_data,
        }
        error_message = (
            f"{error_message} | Failed to load speed tests: {fallback_error}"
            if error_message
            else f"Failed to load speed tests: {fallback_error}"
        )
    context = _base_context(request, db, active_page="speedtests")
    context.update(data)
    context["message"] = message
    filter_error = data.get("invalid_filter_error")
    if filter_error:
        error_message = f"{error_message} | {filter_error}" if error_message else str(filter_error)
    context["error_message"] = error_message
    return templates.TemplateResponse("admin/network/speedtests/index.html", context)


@router.get("/speedtests/analytics", response_class=HTMLResponse)
def speedtests_analytics(
    request: Request,
    days: int = 30,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    data = web_network_speedtests_service.analytics_page_data(db, days=days)
    context = _base_context(request, db, active_page="speedtests")
    context.update(data)
    return templates.TemplateResponse("admin/network/speedtests/analytics.html", context)


@router.get("/speedtests/new", response_class=HTMLResponse)
def speedtests_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context(request, db, active_page="speedtests")
    context.update(
        {
            "speedtest": web_network_speedtests_service.speedtest_form_snapshot(
                {"source": "manual", "tested_at": None}
            ),
            "action_url": "/admin/network/speedtests",
            "error": None,
            **web_network_speedtests_service.speedtest_form_reference_data(db),
        }
    )
    return templates.TemplateResponse("admin/network/speedtests/form.html", context)


@router.post("/speedtests", response_class=HTMLResponse)
def speedtests_create(request: Request, db: Session = Depends(get_db)):
    values = web_network_speedtests_service.parse_speedtest_form(parse_form_data_sync(request))
    error = web_network_speedtests_service.validate_speedtest_values(values)
    if error:
        context = _base_context(request, db, active_page="speedtests")
        context.update(
            {
                "speedtest": web_network_speedtests_service.speedtest_form_snapshot(values),
                "action_url": "/admin/network/speedtests",
                "error": error,
                **web_network_speedtests_service.speedtest_form_reference_data(db),
            }
        )
        return templates.TemplateResponse("admin/network/speedtests/form.html", context)

    try:
        result = web_network_speedtests_service.create_speedtest(db, values)
    except Exception as exc:
        context = _base_context(request, db, active_page="speedtests")
        context.update(
            {
                "speedtest": web_network_speedtests_service.speedtest_form_snapshot(values),
                "action_url": "/admin/network/speedtests",
                "error": str(exc),
                **web_network_speedtests_service.speedtest_form_reference_data(db),
            }
        )
        return templates.TemplateResponse("admin/network/speedtests/form.html", context)

    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="speed_test",
        entity_id=str(result.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "download_mbps": result.download_mbps,
            "upload_mbps": result.upload_mbps,
            "latency_ms": result.latency_ms,
        },
    )
    return RedirectResponse("/admin/network/speedtests", status_code=303)


@router.post("/speedtests/clear-history")
def speedtests_clear_history(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    confirm_text = str(form.get("confirm_text") or "")
    older_than_days = str(form.get("older_than_days") or "").strip()
    parsed_days: int | None = None
    if older_than_days:
        try:
            parsed_days = max(0, int(older_than_days))
        except ValueError:
            return RedirectResponse(
                "/admin/network/speedtests?error_message=Invalid+older-than+days+value",
                status_code=303,
            )
    try:
        deleted = web_network_speedtests_service.clear_history(
            db,
            confirm_text=confirm_text,
            older_than_days=parsed_days,
        )
    except Exception as exc:
        return RedirectResponse(
            f"/admin/network/speedtests?error_message={quote_plus(str(exc))}",
            status_code=303,
        )

    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="delete",
        entity_type="speed_test",
        entity_id=None,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"deleted_rows": deleted, "older_than_days": parsed_days},
    )
    return RedirectResponse(
        f"/admin/network/speedtests?message=Deleted+{deleted}+speed+test+records",
        status_code=303,
    )
