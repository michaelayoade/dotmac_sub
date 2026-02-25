"""Admin network speed test web routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_speedtests as web_network_speedtests_service
from app.services.audit_helpers import log_audit_event
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


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
    search: str | None = None,
    subscriber_id: str | None = None,
    network_device_id: str | None = None,
    pop_site_id: str | None = None,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
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
    context = _base_context(request, db, active_page="speedtests")
    context.update(data)
    return templates.TemplateResponse("admin/network/speedtests/index.html", context)


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
