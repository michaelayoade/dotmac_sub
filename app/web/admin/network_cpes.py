"""Admin network CPE management routes."""

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_cpes as web_network_cpes_service
from app.services.audit_helpers import build_audit_activities, log_audit_event
from app.services.auth_dependencies import require_permission
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


@router.get("/cpes", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def cpe_list(
    request: Request,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    subscriber_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    state = web_network_cpes_service.build_cpe_list_data(
        db,
        search=search,
        status=status,
        vendor=vendor,
        subscriber_id=subscriber_id,
    )
    context = _base_context(request, db, active_page="cpes")
    context.update(state)
    return templates.TemplateResponse("admin/network/cpes/index.html", context)


@router.get("/cpes/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def cpe_new(
    request: Request,
    subscriber_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, active_page="cpes")
    context.update(
        {
            "cpe": web_network_cpes_service.cpe_form_snapshot({"subscriber_id": subscriber_id or ""}),
            "action_url": "/admin/network/cpes",
            **web_network_cpes_service.cpe_form_reference_data(db, subscriber_id=subscriber_id),
            "error": None,
        }
    )
    return templates.TemplateResponse("admin/network/cpes/form.html", context)


@router.post("/cpes", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def cpe_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = web_network_cpes_service.parse_cpe_form(form)
    error = web_network_cpes_service.validate_cpe_values(values)
    if error:
        context = _base_context(request, db, active_page="cpes")
        context.update(
            {
                "cpe": web_network_cpes_service.cpe_form_snapshot(values),
                "action_url": "/admin/network/cpes",
                **web_network_cpes_service.cpe_form_reference_data(
                    db, subscriber_id=str(values.get("subscriber_id") or "")
                ),
                "error": error,
            }
        )
        return templates.TemplateResponse("admin/network/cpes/form.html", context)
    try:
        cpe = web_network_cpes_service.create_cpe(db, values)
    except Exception as exc:
        context = _base_context(request, db, active_page="cpes")
        context.update(
            {
                "cpe": web_network_cpes_service.cpe_form_snapshot(values),
                "action_url": "/admin/network/cpes",
                **web_network_cpes_service.cpe_form_reference_data(
                    db, subscriber_id=str(values.get("subscriber_id") or "")
                ),
                "error": str(exc),
            }
        )
        return templates.TemplateResponse("admin/network/cpes/form.html", context)

    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="cpe",
        entity_id=str(cpe.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"serial_number": cpe.serial_number, "vendor": cpe.vendor},
    )
    return RedirectResponse(f"/admin/network/cpes/{cpe.id}", status_code=303)


@router.get("/cpes/{cpe_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def cpe_edit(request: Request, cpe_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        cpe = web_network_cpes_service.get_cpe(db, cpe_id=cpe_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "CPE not found"},
            status_code=404,
        )
    cpe_data = web_network_cpes_service.cpe_form_snapshot_from_model(cpe)
    context = _base_context(request, db, active_page="cpes")
    context.update(
        {
            "cpe": cpe_data,
            "action_url": f"/admin/network/cpes/{cpe_id}",
            **web_network_cpes_service.cpe_form_reference_data(
                db, subscriber_id=str(cpe_data.get("subscriber_id") or "")
            ),
            "error": None,
        }
    )
    return templates.TemplateResponse("admin/network/cpes/form.html", context)


@router.post("/cpes/{cpe_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def cpe_update(request: Request, cpe_id: str, db: Session = Depends(get_db)):
    try:
        web_network_cpes_service.get_cpe(db, cpe_id=cpe_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "CPE not found"},
            status_code=404,
        )
    form = parse_form_data_sync(request)
    values = web_network_cpes_service.parse_cpe_form(form)
    error = web_network_cpes_service.validate_cpe_values(values)
    if error:
        context = _base_context(request, db, active_page="cpes")
        context.update(
            {
                "cpe": web_network_cpes_service.cpe_form_snapshot(values, cpe_id=cpe_id),
                "action_url": f"/admin/network/cpes/{cpe_id}",
                **web_network_cpes_service.cpe_form_reference_data(
                    db, subscriber_id=str(values.get("subscriber_id") or "")
                ),
                "error": error,
            }
        )
        return templates.TemplateResponse("admin/network/cpes/form.html", context)
    try:
        cpe = web_network_cpes_service.update_cpe(db, cpe_id=cpe_id, values=values)
    except Exception as exc:
        context = _base_context(request, db, active_page="cpes")
        context.update(
            {
                "cpe": web_network_cpes_service.cpe_form_snapshot(values, cpe_id=cpe_id),
                "action_url": f"/admin/network/cpes/{cpe_id}",
                **web_network_cpes_service.cpe_form_reference_data(
                    db, subscriber_id=str(values.get("subscriber_id") or "")
                ),
                "error": str(exc),
            }
        )
        return templates.TemplateResponse("admin/network/cpes/form.html", context)

    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="cpe",
        entity_id=str(cpe.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"serial_number": cpe.serial_number, "vendor": cpe.vendor},
    )
    return RedirectResponse(f"/admin/network/cpes/{cpe.id}", status_code=303)


@router.post("/cpes/{cpe_id}/test-api", dependencies=[Depends(require_permission("network:write"))])
def cpe_test_api(cpe_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        cpe = web_network_cpes_service.get_cpe(db, cpe_id=cpe_id)
    except Exception:
        message = quote_plus("CPE not found")
        return RedirectResponse(
            f"/admin/network/cpes/{cpe_id}?test_status=error&test_message={message}",
            status_code=303,
        )
    meta, _ = web_network_cpes_service.parse_cpe_notes_metadata(cpe.notes)
    api_host = str(meta.get("api_host") or "").strip()
    api_port = str(meta.get("api_port") or "8728").strip()
    api_user = str(meta.get("api_user") or "").strip()
    if "mikrotik" not in str(cpe.vendor or "").lower():
        message = quote_plus("API panel is for MikroTik devices")
        return RedirectResponse(
            f"/admin/network/cpes/{cpe_id}?test_status=error&test_message={message}",
            status_code=303,
        )
    if not api_host:
        message = quote_plus("API host is not configured")
        return RedirectResponse(
            f"/admin/network/cpes/{cpe_id}?test_status=error&test_message={message}",
            status_code=303,
        )
    message = f"API configuration looks valid ({api_host}:{api_port}) user={api_user or 'n/a'}"
    encoded_message = quote_plus(message)
    return RedirectResponse(
        f"/admin/network/cpes/{cpe_id}?test_status=success&test_message={encoded_message}",
        status_code=303,
    )


@router.get("/cpes/{cpe_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def cpe_detail(
    request: Request,
    cpe_id: str,
    test_status: str | None = None,
    test_message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        cpe = web_network_cpes_service.get_cpe(db, cpe_id=cpe_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "CPE not found"},
            status_code=404,
        )
    meta, cleaned_notes = web_network_cpes_service.parse_cpe_notes_metadata(cpe.notes)
    activities = build_audit_activities(db, "cpe", str(cpe_id), limit=20)
    context = _base_context(request, db, active_page="cpes")
    context.update(
        {
            "cpe": cpe,
            "cpe_meta": meta,
            "cpe_notes": cleaned_notes,
            "is_mikrotik": "mikrotik" in str(cpe.vendor or "").lower(),
            "activities": activities,
            "test_status": test_status,
            "test_message": test_message,
        }
    )
    return templates.TemplateResponse("admin/network/cpes/detail.html", context)
