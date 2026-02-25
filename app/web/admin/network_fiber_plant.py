"""Admin network fiber plant web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_fdh as web_network_fdh_service
from app.services import web_network_fiber as web_network_fiber_service
from app.services import web_network_fiber_plant as web_network_fiber_plant_service
from app.services.audit_helpers import build_audit_activities, log_audit_event
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync, parse_json_body_sync

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


@router.get("/fiber-plant", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def fiber_plant_consolidated(
    request: Request,
    tab: str = "cabinets",
    db: Session = Depends(get_db),
):
    """Consolidated view of fiber plant infrastructure."""
    page_data = web_network_fiber_service.get_fiber_plant_consolidated_data(db)
    context = _base_context(request, db, active_page="fiber-plant", active_menu="fiber")
    context.update({"tab": tab, **page_data})
    return templates.TemplateResponse("admin/network/fiber-plant/index.html", context)


@router.get("/fiber-map", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def fiber_plant_map(request: Request, db: Session = Depends(get_db)):
    """Interactive fiber plant map."""
    page_data = web_network_fiber_service.get_fiber_plant_map_data(db)
    context = _base_context(request, db, active_page="fiber-map", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/map.html", context)


@router.get("/fiber-change-requests", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def fiber_change_requests(request: Request, db: Session = Depends(get_db)):
    """Review pending vendor fiber change requests."""
    page_data = web_network_fiber_plant_service.change_requests_page_data(
        db,
        bulk_status=request.query_params.get("bulk"),
        skipped=request.query_params.get("skipped"),
    )
    context = _base_context(request, db, active_page="fiber-change-requests", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse(
        "admin/network/fiber/change_requests.html", context
    )


@router.get("/fiber-change-requests/{request_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def fiber_change_request_detail(request: Request, request_id: str, db: Session = Depends(get_db)):
    """Review a specific fiber change request."""
    page_data = web_network_fiber_plant_service.change_request_detail_page_data(
        db,
        request_id=request_id,
        error=request.query_params.get("error"),
    )
    context = _base_context(request, db, active_page="fiber-change-requests", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse(
        "admin/network/fiber/change_request_detail.html", context
    )


@router.post("/fiber-change-requests/{request_id}/approve", dependencies=[Depends(require_permission("network:write"))])
def fiber_change_request_approve(request: Request, request_id: str, db: Session = Depends(get_db)):
    data = parse_form_data_sync(request)
    review_notes = web_network_fiber_plant_service.form_optional_str(data, "review_notes")
    force_apply = data.get("force_apply") == "true"
    current_user = _base_context(request, db, active_page="fiber-change-requests")["current_user"]
    approved, error = web_network_fiber_plant_service.approve_change_request(
        db,
        request_id=request_id,
        reviewer_person_id=current_user["person_id"],
        review_notes=review_notes,
        force_apply=force_apply,
    )
    if not approved and error == "conflict":
        return RedirectResponse(
            url=f"/admin/network/fiber-change-requests/{request_id}?error=conflict",
            status_code=303,
        )
    log_audit_event(
        db=db,
        request=request,
        action="approve",
        entity_type="fiber_change_request",
        entity_id=str(request_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"force_apply": force_apply, "review_notes": review_notes},
    )
    return RedirectResponse(
        url=f"/admin/network/fiber-change-requests/{request_id}", status_code=303
    )


@router.post("/fiber-change-requests/{request_id}/reject", dependencies=[Depends(require_permission("network:write"))])
def fiber_change_request_reject(request: Request, request_id: str, db: Session = Depends(get_db)):
    data = parse_form_data_sync(request)
    review_notes = web_network_fiber_plant_service.form_optional_str(data, "review_notes")
    current_user = _base_context(request, db, active_page="fiber-change-requests")["current_user"]
    error = web_network_fiber_plant_service.reject_change_request(
        db,
        request_id=request_id,
        reviewer_person_id=current_user["person_id"],
        review_notes=review_notes,
    )
    if error == "reject_note_required":
        return RedirectResponse(
            url=f"/admin/network/fiber-change-requests/{request_id}?error=reject_note_required",
            status_code=303,
        )
    log_audit_event(
        db=db,
        request=request,
        action="reject",
        entity_type="fiber_change_request",
        entity_id=str(request_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"review_notes": review_notes},
    )
    return RedirectResponse(
        url=f"/admin/network/fiber-change-requests/{request_id}", status_code=303
    )


@router.post("/fiber-change-requests/bulk-approve", dependencies=[Depends(require_permission("network:write"))])
def fiber_change_requests_bulk_approve(request: Request, db: Session = Depends(get_db)):
    data = parse_form_data_sync(request)
    request_ids = web_network_fiber_plant_service.form_getlist_str(data, "request_ids")
    force_apply = data.get("force_apply") == "true"
    current_user = _base_context(request, db, active_page="fiber-change-requests")["current_user"]
    result = web_network_fiber_plant_service.bulk_approve_change_requests(
        db,
        request_ids=request_ids,
        reviewer_person_id=current_user["person_id"],
        force_apply=force_apply,
    )
    approved_request_ids = result["approved_request_ids"]
    for request_id in approved_request_ids:
        log_audit_event(
            db=db,
            request=request,
            action="approve",
            entity_type="fiber_change_request",
            entity_id=str(request_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"force_apply": force_apply, "review_notes": "Bulk approved"},
        )
    return RedirectResponse(
        url=f"/admin/network/fiber-change-requests?bulk=approved&skipped={result['skipped']}",
        status_code=303,
    )


@router.post("/fiber-map/save-plan", dependencies=[Depends(require_permission("network:write"))])
def fiber_map_save_plan(request: Request, db: Session = Depends(get_db)):
    """Persist a planned route to a project quote."""
    # vendor_service removed during CRM cleanup - this endpoint is disabled
    return JSONResponse({"error": "Route revision feature not available"}, status_code=501)


@router.post("/fiber-map/update-position", dependencies=[Depends(require_permission("network:write"))])
def update_asset_position(request: Request, db: Session = Depends(get_db)):
    """Update position of FDH cabinet or splice closure via drag-and-drop."""
    data: dict[str, object] = parse_json_body_sync(request)
    payload, status_code = web_network_fiber_plant_service.update_asset_position_data(db, data)
    return JSONResponse(payload, status_code=status_code)


@router.get("/fiber-map/nearest-cabinet", dependencies=[Depends(require_permission("network:read"))])
def find_nearest_cabinet(request: Request, lat: float, lng: float, db: Session = Depends(get_db)):
    """Find nearest FDH cabinet to given coordinates for installation planning."""
    payload, status_code = web_network_fiber_service.find_nearest_cabinet_data(
        db,
        lat=lat,
        lng=lng,
    )
    return JSONResponse(payload, status_code=status_code)


@router.get("/fiber-map/plan-options", dependencies=[Depends(require_permission("network:read"))])
def plan_options(request: Request, lat: float, lng: float, db: Session = Depends(get_db)):
    """List nearby cabinets for planning and manual routing."""
    payload, status_code = web_network_fiber_service.get_plan_options_data(
        db,
        lat=lat,
        lng=lng,
    )
    return JSONResponse(payload, status_code=status_code)


@router.get("/fiber-map/route", dependencies=[Depends(require_permission("network:read"))])
def plan_route(request: Request, lat: float, lng: float, cabinet_id: str, db: Session = Depends(get_db)):
    """Calculate a fiber route between a point and a cabinet."""
    payload, status_code = web_network_fiber_service.get_plan_route_data(
        db,
        lat=lat,
        lng=lng,
        cabinet_id=cabinet_id,
    )
    return JSONResponse(payload, status_code=status_code)


@router.get("/fiber-reports", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def fiber_reports(request: Request, db: Session = Depends(get_db), map_limit: int | None = None):
    """Fiber network deployment reports with asset statistics and customer map."""
    page_data = web_network_fiber_service.get_fiber_reports_data(db, map_limit)
    context = _base_context(request, db, active_page="fiber-reports", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/reports.html", context)


@router.get("/fdh-cabinets", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def fdh_cabinets_list(request: Request, db: Session = Depends(get_db)):
    """List FDH cabinets."""
    page_data = web_network_fdh_service.list_page_data(db)
    context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/fdh-cabinets.html", context)


@router.get("/fdh-cabinets/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def fdh_cabinet_new(request: Request, db: Session = Depends(get_db)):
    form_context = web_network_fdh_service.build_form_context(
        db,
        cabinet=None,
        action_url="/admin/network/fdh-cabinets",
    )

    context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/fdh-cabinet-form.html", context)


@router.post("/fdh-cabinets", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def fdh_cabinet_create(request: Request, db: Session = Depends(get_db)):
    result = web_network_fdh_service.create_cabinet_submission(
        db,
        parse_form_data_sync(request),
        action_url="/admin/network/fdh-cabinets",
    )
    if result["error"]:
        context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
        context.update(result["form_context"])
        return templates.TemplateResponse("admin/network/fiber/fdh-cabinet-form.html", context)

    cabinet = result["cabinet"]
    assert cabinet is not None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="fdh_cabinet",
        entity_id=str(cabinet.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": cabinet.name, "code": cabinet.code},
    )

    return RedirectResponse(f"/admin/network/fdh-cabinets/{cabinet.id}", status_code=303)


@router.get("/fdh-cabinets/{cabinet_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def fdh_cabinet_edit(request: Request, cabinet_id: str, db: Session = Depends(get_db)):
    cabinet = web_network_fdh_service.get_cabinet(db, cabinet_id)
    if not cabinet:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "FDH Cabinet not found"},
            status_code=404,
        )

    form_context = web_network_fdh_service.build_form_context(
        db,
        cabinet=cabinet,
        action_url=f"/admin/network/fdh-cabinets/{cabinet.id}",
    )
    context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/fdh-cabinet-form.html", context)


@router.post("/fdh-cabinets/{cabinet_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def fdh_cabinet_update(request: Request, cabinet_id: str, db: Session = Depends(get_db)):
    cabinet = web_network_fdh_service.get_cabinet(db, cabinet_id)
    if not cabinet:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "FDH Cabinet not found"},
            status_code=404,
        )

    result = web_network_fdh_service.update_cabinet_submission(
        db,
        cabinet,
        parse_form_data_sync(request),
        action_url=f"/admin/network/fdh-cabinets/{cabinet.id}",
    )
    if result["error"]:
        context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
        context.update(result["form_context"])
        return templates.TemplateResponse("admin/network/fiber/fdh-cabinet-form.html", context)

    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="fdh_cabinet",
        entity_id=str(cabinet.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=result["metadata"],
    )

    return RedirectResponse(f"/admin/network/fdh-cabinets/{cabinet.id}", status_code=303)


@router.get("/fdh-cabinets/{cabinet_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def fdh_cabinet_detail(request: Request, cabinet_id: str, db: Session = Depends(get_db)):
    page_data = web_network_fdh_service.detail_page_data(db, cabinet_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "FDH Cabinet not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
    context.update(page_data)
    context["activities"] = build_audit_activities(db, "fdh_cabinet", str(cabinet_id), limit=10)
    return templates.TemplateResponse("admin/network/fiber/fdh-cabinet-detail.html", context)


@router.get("/splitters", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def splitters_list(request: Request, db: Session = Depends(get_db)):
    page_data = web_network_fdh_service.list_splitters_page_data(db)
    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/splitters.html", context)


@router.get("/splitters/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def splitter_new(request: Request, fdh_id: str | None = None, db: Session = Depends(get_db)):
    form_context = web_network_fdh_service.build_splitter_form_context(
        db,
        splitter=None,
        action_url="/admin/network/splitters",
        selected_fdh_id=fdh_id,
    )
    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)


@router.post("/splitters", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def splitter_create(request: Request, db: Session = Depends(get_db)):
    result = web_network_fdh_service.create_splitter_submission(
        db,
        parse_form_data_sync(request),
        action_url="/admin/network/splitters",
    )
    if result["error"]:
        context = _base_context(request, db, active_page="splitters", active_menu="fiber")
        context.update(result["form_context"])
        return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)

    splitter = result["splitter"]
    assert splitter is not None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="splitter",
        entity_id=str(splitter.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": splitter.name, "fdh_id": str(splitter.fdh_id) if splitter.fdh_id else None},
    )

    return RedirectResponse(f"/admin/network/splitters/{splitter.id}", status_code=303)


@router.get("/splitters/{splitter_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def splitter_edit(request: Request, splitter_id: str, db: Session = Depends(get_db)):
    splitter = web_network_fdh_service.get_splitter(db, splitter_id)
    if not splitter:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splitter not found"},
            status_code=404,
        )

    form_context = web_network_fdh_service.build_splitter_form_context(
        db,
        splitter=splitter,
        action_url=f"/admin/network/splitters/{splitter.id}",
        selected_fdh_id=str(splitter.fdh_id) if splitter.fdh_id else None,
    )
    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)


@router.post("/splitters/{splitter_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def splitter_update(request: Request, splitter_id: str, db: Session = Depends(get_db)):
    splitter = web_network_fdh_service.get_splitter(db, splitter_id)
    if not splitter:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splitter not found"},
            status_code=404,
        )

    result = web_network_fdh_service.update_splitter_submission(
        db,
        splitter,
        parse_form_data_sync(request),
        action_url=f"/admin/network/splitters/{splitter.id}",
    )
    if result["error"]:
        context = _base_context(request, db, active_page="splitters", active_menu="fiber")
        context.update(result["form_context"])
        return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)

    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="splitter",
        entity_id=str(splitter.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=result["metadata"],
    )

    return RedirectResponse(f"/admin/network/splitters/{splitter.id}", status_code=303)


@router.get("/splitters/{splitter_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def splitter_detail(request: Request, splitter_id: str, db: Session = Depends(get_db)):
    page_data = web_network_fdh_service.splitter_detail_page_data(db, splitter_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splitter not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update(page_data)
    context["activities"] = build_audit_activities(db, "splitter", str(splitter_id), limit=10)
    return templates.TemplateResponse("admin/network/fiber/splitter-detail.html", context)
