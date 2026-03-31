"""Admin web routes for ONT service-port management."""

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_service_ports as web_network_service_ports_service
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network-ont-service-ports"])


def _base_context(request: Request, db: Session, active_page: str) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "network",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _service_ports_partial_response(
    request: Request,
    db: Session,
    ont_id: str,
    *,
    toast_message: str | None = None,
    toast_type: str = "success",
) -> HTMLResponse:
    context = web_network_service_ports_service.list_context(db, ont_id)
    response = templates.TemplateResponse(
        "admin/network/onts/_service_ports_tab.html",
        {"request": request, **context},
    )
    if toast_message:
        response.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": toast_message, "type": toast_type}}
        )
    return response


@router.get(
    "/onts/{ont_id}/service-ports",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_service_ports(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: Service-ports tab for ONT detail page."""
    data = web_network_service_ports_service.list_context(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/onts/_service_ports_tab.html", context
    )


@router.post(
    "/onts/{ont_id}/service-ports/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_service_port_create(
    request: Request,
    ont_id: str,
    vlan_id: int = Form(...),
    gem_index: int = Form(default=1),
    user_vlan: str = Form(default=""),
    tag_transform: str = Form(default="translate"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Create a single service-port on the OLT for this ONT."""
    resolved_user_vlan: int | str | None = None
    raw_user_vlan = user_vlan.strip()
    if raw_user_vlan:
        if raw_user_vlan == "untagged":
            resolved_user_vlan = "untagged"
        else:
            try:
                resolved_user_vlan = int(raw_user_vlan)
            except ValueError:
                return _service_ports_partial_response(
                    request,
                    db,
                    ont_id,
                    toast_message="User VLAN must be a number or 'untagged'",
                    toast_type="error",
                )

    ok, msg = web_network_service_ports_service.handle_create(
        db,
        ont_id,
        vlan_id,
        gem_index,
        user_vlan=resolved_user_vlan,
        tag_transform=tag_transform,
    )
    return _service_ports_partial_response(
        request,
        db,
        ont_id,
        toast_message=msg,
        toast_type="success" if ok else "error",
    )


@router.post(
    "/onts/{ont_id}/service-ports/{index}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_service_port_delete(
    request: Request,
    ont_id: str,
    index: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Delete a service-port from the OLT by index."""
    ok, msg = web_network_service_ports_service.handle_delete(db, ont_id, index)
    return _service_ports_partial_response(
        request,
        db,
        ont_id,
        toast_message=msg,
        toast_type="success" if ok else "error",
    )


@router.post(
    "/onts/{ont_id}/service-ports/clone",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_service_port_clone(
    request: Request,
    ont_id: str,
    ref_ont_id: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Clone service-ports from a reference ONT."""
    ok, msg = web_network_service_ports_service.handle_clone(db, ont_id, ref_ont_id)
    return _service_ports_partial_response(
        request,
        db,
        ont_id,
        toast_message=msg,
        toast_type="success" if ok else "error",
    )
