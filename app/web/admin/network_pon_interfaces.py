"""Admin web routes for PON interface inventory and description management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_admin as web_admin_service
from app.services import (
    web_network_pon_interfaces as web_network_pon_interfaces_service,
)
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network-pon-interfaces"])


def _base_context(request: Request, db: Session, active_page: str) -> dict:
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "network",
        "current_user": web_admin_service.get_current_user(request),
        "sidebar_stats": web_admin_service.get_sidebar_stats(db),
    }


@router.get(
    "/pon-interfaces",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def pon_interfaces_list(
    request: Request,
    search: str | None = None,
    status: str | None = None,
    olt_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, active_page="pon-interfaces")
    context.update(
        web_network_pon_interfaces_service.build_page_data(
            db,
            search=search,
            status=status,
            olt_id=olt_id,
        )
    )
    return templates.TemplateResponse(
        "admin/network/pon_interfaces/index.html", context
    )


@router.post(
    "/pon-interfaces/description",
    dependencies=[Depends(require_permission("network:write"))],
)
def pon_interface_save_description(
    request: Request,
    olt_id: str = Form(""),
    interface_name: str = Form(""),
    description: str = Form(""),
    pon_port_id: str = Form(""),
    return_to: str = Form("/admin/network/pon-interfaces"),
    db: Session = Depends(get_db),
) -> Response:
    port = web_network_pon_interfaces_service.save_description(
        db,
        olt_id=olt_id,
        interface_name=interface_name,
        description=description,
        pon_port_id=pon_port_id or None,
    )
    # HTMX request — return 200 with toast trigger
    if request.headers.get("HX-Request"):
        saved_desc = port.description or ""
        return Response(
            content=saved_desc,
            headers={"HX-Trigger": '{"showToast": "Description saved"}'},
        )
    target = return_to or "/admin/network/pon-interfaces"
    return RedirectResponse(url=target, status_code=303)
