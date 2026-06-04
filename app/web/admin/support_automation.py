"""Admin web routes for ticket automation rules."""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.support import (
    AutomationActionType,
    AutomationTrigger,
)
from app.services import support_automation as automation_service
from app.services import support_ticket_settings as support_ticket_settings_service
from app.services.auth_dependencies import require_permission

router = APIRouter(prefix="/support/automation", tags=["web-admin-support-automation"])
templates = Jinja2Templates(directory="templates")


def _ctx(request: Request, db: Session) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "support-automation",
        "active_menu": "services",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "triggers": [t.value for t in AutomationTrigger],
        "action_types": [a.value for a in AutomationActionType],
        "priorities": support_ticket_settings_service.list_priority_options(db),
        "statuses": support_ticket_settings_service.list_status_options(db),
    }


def _parse_json_field(raw: str | None, field: str) -> dict:
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must be a JSON object")
    return parsed


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def automation_list(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db)
    context["rules"] = automation_service.list_rules(db)
    return templates.TemplateResponse("admin/support/automation/index.html", context)


@router.get(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def automation_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db)
    context.update({"rule": None, "form_mode": "create", "error": None})
    return templates.TemplateResponse("admin/support/automation/form.html", context)


@router.post(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def automation_create(
    request: Request,
    name: str = Form(...),
    description: str | None = Form(None),
    trigger: str = Form(...),
    action_type: str = Form(...),
    conditions_json: str | None = Form(None),
    action_value_json: str | None = Form(None),
    sort_order: int = Form(100),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        conditions = _parse_json_field(conditions_json, "conditions")
        action_value = _parse_json_field(action_value_json, "action_value")
        automation_service.create_rule(
            db,
            name=name,
            description=description,
            trigger=AutomationTrigger(trigger),
            action_type=AutomationActionType(action_type),
            conditions=conditions,
            action_value=action_value,
            sort_order=sort_order,
            is_active=is_active,
        )
        db.commit()
    except (ValueError, KeyError) as exc:
        db.rollback()
        context = _ctx(request, db)
        context.update(
            {
                "rule": None,
                "form_mode": "create",
                "error": str(exc),
                "form_values": {
                    "name": name,
                    "description": description,
                    "trigger": trigger,
                    "action_type": action_type,
                    "conditions_json": conditions_json,
                    "action_value_json": action_value_json,
                    "sort_order": sort_order,
                    "is_active": is_active,
                },
            }
        )
        return templates.TemplateResponse(
            "admin/support/automation/form.html", context, status_code=400
        )
    return RedirectResponse(url="/admin/support/automation", status_code=303)


@router.get(
    "/{rule_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def automation_edit_page(
    request: Request, rule_id: UUID, db: Session = Depends(get_db)
):
    rule = automation_service.get_rule(db, str(rule_id))
    context = _ctx(request, db)
    context.update({"rule": rule, "form_mode": "edit", "error": None})
    return templates.TemplateResponse("admin/support/automation/form.html", context)


@router.post(
    "/{rule_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def automation_update(
    request: Request,
    rule_id: UUID,
    name: str = Form(...),
    description: str | None = Form(None),
    trigger: str = Form(...),
    action_type: str = Form(...),
    conditions_json: str | None = Form(None),
    action_value_json: str | None = Form(None),
    sort_order: int = Form(100),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        conditions = _parse_json_field(conditions_json, "conditions")
        action_value = _parse_json_field(action_value_json, "action_value")
        automation_service.update_rule(
            db,
            str(rule_id),
            name=name,
            description=description,
            trigger=AutomationTrigger(trigger),
            action_type=AutomationActionType(action_type),
            conditions=conditions,
            action_value=action_value,
            sort_order=sort_order,
            is_active=is_active,
        )
        db.commit()
    except (ValueError, KeyError) as exc:
        db.rollback()
        rule = automation_service.get_rule(db, str(rule_id))
        context = _ctx(request, db)
        context.update({"rule": rule, "form_mode": "edit", "error": str(exc)})
        return templates.TemplateResponse(
            "admin/support/automation/form.html", context, status_code=400
        )
    return RedirectResponse(url="/admin/support/automation", status_code=303)


@router.post(
    "/{rule_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def automation_delete(rule_id: UUID, db: Session = Depends(get_db)):
    automation_service.delete_rule(db, str(rule_id))
    db.commit()
    return RedirectResponse(url="/admin/support/automation", status_code=303)


@router.post(
    "/{rule_id}/toggle",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def automation_toggle(
    rule_id: UUID,
    target: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """Toggle a rule's active state.

    If `target=on` or `target=off` is supplied, the call is idempotent
    (double-click won't flip back). Otherwise falls back to legacy flip.
    """
    if target in ("on", "off"):
        automation_service.set_rule_active(db, str(rule_id), is_active=(target == "on"))
    else:
        automation_service.toggle_rule(db, str(rule_id))
    db.commit()
    return RedirectResponse(url="/admin/support/automation", status_code=303)
