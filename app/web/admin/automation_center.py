"""Permission-gated Automation Center admin hub."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import automation_rules, service_team_lifecycle, web_automation_center
from app.services.auth_dependencies import has_permission, require_permission
from app.services.automation_rules import RULE_CREATE_PERMISSION, RULE_READ_PERMISSION
from app.services.automation_runtime import RUN_READ_PERMISSION
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/automation", tags=["web-admin-automation"])

_PILOT_TRIGGER = "support.ticket.created"
_PILOT_ACTION = "support.ticket.assign_service_team"
_KEY_WORDS = re.compile(r"[^a-z0-9]+")


def _base_context(request: Request, db: Session) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "automation-center",
        "active_menu": "automation-center",
        "page_title": "Automation Center",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _actor(request: Request) -> str:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    principal_type = str(current_user.get("principal_type") or "system_user")
    principal_id = str(
        current_user.get("principal_id") or current_user.get("id") or "unknown"
    )
    return f"automation-admin:{principal_type}:{principal_id}"[:255]


def _pilot_rule_key(name: str) -> str:
    normalized = _KEY_WORDS.sub("_", name.strip().casefold()).strip("_")
    if not normalized:
        raise ValueError("Rule name is required.")
    return f"support.ticket.assignment.{normalized}"[:120].rstrip("_")


def _pilot_form_context(
    request: Request,
    db: Session,
    *,
    error: str | None = None,
    name: str = "",
    service_team_id: str = "",
) -> dict[str, object]:
    return {
        **_base_context(request, db),
        "error": error,
        "name": name,
        "service_team_id": service_team_id,
        "service_teams": service_team_lifecycle.list_active_team_options(db),
    }


@router.get("", response_class=HTMLResponse)
def automation_center_index(
    request: Request,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("automation:hub:read")),
):
    """Show registry, rules, run evidence, and legacy ownership in one place."""

    state = web_automation_center.build_automation_center_data(
        db,
        can_read_rules=has_permission(auth, db, RULE_READ_PERMISSION),
        can_read_runs=has_permission(auth, db, RUN_READ_PERMISSION),
        can_create_rules=has_permission(auth, db, RULE_CREATE_PERMISSION),
        can_read_support_tickets=has_permission(auth, db, "support:ticket:read"),
        can_update_support_tickets=has_permission(auth, db, "support:ticket:update"),
    )
    return templates.TemplateResponse(
        "admin/automation/index.html",
        {**_base_context(request, db), **state},
    )


@router.get(
    "/ticket-assignment/new",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(RULE_CREATE_PERMISSION)),
        Depends(require_permission("support:ticket:read")),
        Depends(require_permission("support:ticket:update")),
    ],
)
def new_ticket_assignment_draft(request: Request, db: Session = Depends(get_db)):
    """Render the single, non-executable ticket-assignment pilot form."""

    return templates.TemplateResponse(
        "admin/automation/ticket_assignment_draft.html",
        _pilot_form_context(request, db),
    )


@router.post(
    "/ticket-assignment/drafts",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission("support:ticket:read")),
        Depends(require_permission("support:ticket:update")),
    ],
)
def create_ticket_assignment_draft(
    request: Request,
    name: str = Form(...),
    service_team_id: str = Form(...),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(RULE_CREATE_PERMISSION)),
):
    """Save a fixed urgent-ticket assignment as a draft only."""

    active_teams = service_team_lifecycle.list_active_team_options(db)
    selected_team_id = next(
        (
            team_id
            for team_id, _label in active_teams
            if str(team_id) == service_team_id
        ),
        None,
    )
    if selected_team_id is None:
        return templates.TemplateResponse(
            "admin/automation/ticket_assignment_draft.html",
            _pilot_form_context(
                request,
                db,
                error="Choose an active service team.",
                name=name,
                service_team_id=service_team_id,
            ),
            status_code=400,
        )
    try:
        key = _pilot_rule_key(name)
        db_session_adapter.release_read_transaction(db)
        automation_rules.create_rule(
            db,
            automation_rules.CreateAutomationRuleCommand(
                tenant_id=web_automation_center.OPERATOR_TENANT_ID,
                key=key,
                name=name,
                description=(
                    "Draft pilot: assign newly created urgent support tickets to a service team."
                ),
                trigger_key=_PILOT_TRIGGER,
                conditions=(
                    automation_rules.AutomationCondition(
                        field_key="priority",
                        operator=automation_rules.AutomationOperator.equals,
                        value="urgent",
                    ),
                ),
                actions=(
                    automation_rules.AutomationActionStep(
                        action_key=_PILOT_ACTION,
                        inputs=(
                            automation_rules.AutomationActionValue(
                                key="service_team_id", value=selected_team_id
                            ),
                        ),
                    ),
                ),
                permission_keys=frozenset(auth.get("permission_keys") or ()),
                context=CommandContext.system(
                    actor=_actor(request),
                    scope="automation:rule:create",
                    reason="Administrator saved a ticket-assignment automation draft",
                    idempotency_key=f"automation-ticket-assignment-draft:{key}",
                ),
            ),
        )
    except (DomainError, ValueError) as exc:
        return templates.TemplateResponse(
            "admin/automation/ticket_assignment_draft.html",
            _pilot_form_context(
                request,
                db,
                error=str(exc),
                name=name,
                service_team_id=service_team_id,
            ),
            status_code=400,
        )
    return RedirectResponse(url="/admin/automation", status_code=303)


__all__ = ["router"]
