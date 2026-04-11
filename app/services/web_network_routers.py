"""Web helpers for admin router management routes."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.requests import Request

from app.schemas.router_management import RouterCreate, RouterUpdate
from app.services.router_management.config import (
    RouterConfigService,
    RouterTemplateService,
)
from app.services.router_management.inventory import JumpHostInventory, RouterInventory
from app.services.router_management.monitoring import RouterMonitoringService


def _base_context(
    request: Request,
    db: Session,
    *,
    active_page: str = "routers",
) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "network",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def list_context(
    request: Request,
    db: Session,
    *,
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, object]:
    context = _base_context(request, db)
    context.update(
        {
            "routers": RouterInventory.list(
                db, status=status, search=search, limit=limit, offset=offset
            ),
            "status_filter": status,
            "search": search or "",
            "summary": RouterMonitoringService.get_dashboard_summary(db),
        }
    )
    return context


def dashboard_context(request: Request, db: Session) -> dict[str, object]:
    context = _base_context(request, db)
    context.update(
        {
            "summary": RouterMonitoringService.get_dashboard_summary(db),
            "recent_pushes": RouterConfigService.list_pushes(db, limit=10),
        }
    )
    return context


def create_form_context(request: Request, db: Session) -> dict[str, object]:
    context = _base_context(request, db)
    context.update({"jump_hosts": JumpHostInventory.list(db), "router": None})
    return context


def template_list_context(
    request: Request,
    db: Session,
    *,
    category: str | None = None,
) -> dict[str, object]:
    context = _base_context(request, db)
    context.update(
        {
            "templates": RouterTemplateService.list(db, category=category),
            "category_filter": category,
        }
    )
    return context


def template_form_context(request: Request, db: Session) -> dict[str, object]:
    context = _base_context(request, db)
    context["template"] = None
    return context


def push_wizard_context(request: Request, db: Session) -> dict[str, object]:
    context = _base_context(request, db)
    context.update(
        {
            "routers": RouterInventory.list(db, limit=200),
            "templates": RouterTemplateService.list(db),
        }
    )
    return context


def push_detail_context(
    request: Request,
    db: Session,
    *,
    push_id: uuid.UUID,
) -> dict[str, object]:
    context = _base_context(request, db)
    push = RouterConfigService.get_push(db, push_id)
    context.update({"push": push, "results": push.results})
    return context


def jump_host_list_context(request: Request, db: Session) -> dict[str, object]:
    context = _base_context(request, db)
    context["jump_hosts"] = JumpHostInventory.list(db)
    return context


def detail_context(
    request: Request,
    db: Session,
    *,
    router_id: uuid.UUID,
    tab: str = "overview",
) -> dict[str, object]:
    context = _base_context(request, db)
    router = RouterInventory.get(db, router_id)
    context.update({"router": router, "tab": tab})

    if tab == "interfaces":
        context["interfaces"] = RouterInventory.list_interfaces(db, router_id)
    elif tab == "config":
        context["snapshots"] = RouterConfigService.list_snapshots(
            db, router_id, limit=20
        )
    elif tab == "pushes":
        context["push_results"] = RouterConfigService.list_push_results(
            db, router_id, limit=20
        )

    return context


def edit_form_context(
    request: Request,
    db: Session,
    *,
    router_id: uuid.UUID,
) -> dict[str, object]:
    context = _base_context(request, db)
    context.update(
        {
            "router": RouterInventory.get(db, router_id),
            "jump_hosts": JumpHostInventory.list(db),
        }
    )
    return context


def _form_strings(form: FormData) -> dict[str, Any]:
    return {key: value for key, value in form.items() if isinstance(value, str)}


def create_router(db: Session, form: FormData):
    payload = RouterCreate(**_form_strings(form))
    return RouterInventory.create(db, payload)


def update_router(db: Session, router_id: uuid.UUID, form: FormData) -> None:
    payload = RouterUpdate(**_form_strings(form))
    RouterInventory.update(db, router_id, payload)
