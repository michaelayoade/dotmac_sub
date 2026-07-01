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


def _validate_action_value(
    db: Session, action_type: AutomationActionType, action_value: dict
) -> dict:
    if action_type == AutomationActionType.assign_team:
        team_id = str(action_value.get("service_team_id") or "").strip()
        if not team_id:
            raise ValueError("action_value.service_team_id is required.")
        try:
            UUID(team_id)
        except ValueError as exc:
            raise ValueError("action_value.service_team_id must be a valid UUID.") from exc
        configured = {
            item["id"] for item in support_ticket_settings_service.list_service_teams(db)
        }
        if team_id not in configured:
            raise ValueError("action_value.service_team_id must match a configured service team.")
        return {"service_team_id": team_id}
    if action_type == AutomationActionType.assign_technician:
        person_id = str(action_value.get("technician_person_id") or "").strip()
        if not person_id:
            raise ValueError("action_value.technician_person_id is required.")
        try:
            UUID(person_id)
        except ValueError as exc:
            raise ValueError(
                "action_value.technician_person_id must be a valid UUID."
            ) from exc
        return {"technician_person_id": person_id}
    if action_type == AutomationActionType.set_priority:
        priority = str(action_value.get("priority") or "").strip()
        if priority not in support_ticket_settings_service.list_priority_options(db):
            raise ValueError("action_value.priority must be a configured priority.")
        return {"priority": priority}
    if action_type == AutomationActionType.set_status:
        status = str(action_value.get("status") or "").strip()
        if status not in support_ticket_settings_service.list_status_options(db):
            raise ValueError("action_value.status must be a configured status.")
        return {"status": status}
    if action_type == AutomationActionType.set_due_in_hours:
        raw_hours = action_value.get("hours")
        try:
            hours = int(raw_hours)
        except (TypeError, ValueError) as exc:
            raise ValueError("action_value.hours must be a whole number.") from exc
        if hours <= 0:
            raise ValueError("action_value.hours must be greater than zero.")
        return {"hours": hours}
    if action_type == AutomationActionType.add_tag:
        tag = str(action_value.get("tag") or "").strip()
        if not tag:
            raise ValueError("action_value.tag is required.")
        return {"tag": tag}
    return action_value


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:automation:read"))],
)
def automation_list(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db)
    context["rules"] = automation_service.list_rules(db)
    return templates.TemplateResponse("admin/support/automation/index.html", context)


@router.get(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:automation:write"))],
)
def automation_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db)
    context.update({"rule": None, "form_mode": "create", "error": None})
    return templates.TemplateResponse("admin/support/automation/form.html", context)


@router.post(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:automation:write"))],
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
        action = AutomationActionType(action_type)
        action_value = _validate_action_value(
            db, action, _parse_json_field(action_value_json, "action_value")
        )
        automation_service.create_rule(
            db,
            name=name,
            description=description,
            trigger=AutomationTrigger(trigger),
            action_type=action,
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
    dependencies=[Depends(require_permission("support:automation:write"))],
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
    dependencies=[Depends(require_permission("support:automation:write"))],
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
        action = AutomationActionType(action_type)
        action_value = _validate_action_value(
            db, action, _parse_json_field(action_value_json, "action_value")
        )
        automation_service.update_rule(
            db,
            str(rule_id),
            name=name,
            description=description,
            trigger=AutomationTrigger(trigger),
            action_type=action,
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
    dependencies=[Depends(require_permission("support:automation:write"))],
)
def automation_delete(rule_id: UUID, db: Session = Depends(get_db)):
    automation_service.delete_rule(db, str(rule_id))
    db.commit()
    return RedirectResponse(url="/admin/support/automation", status_code=303)


@router.post(
    "/{rule_id}/toggle",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:automation:write"))],
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
