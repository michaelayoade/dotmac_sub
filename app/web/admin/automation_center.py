"""Permission-gated Automation Center admin hub."""

from __future__ import annotations

import re
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import (
    automation_rules,
    customer_search,
    service_team_lifecycle,
    web_automation_center,
)
from app.services.auth_dependencies import has_permission, require_permission
from app.services.automation_rules import (
    RULE_CREATE_PERMISSION,
    RULE_OPERATE_PERMISSION,
    RULE_PUBLISH_PERMISSION,
    RULE_READ_PERMISSION,
    RULE_UPDATE_PERMISSION,
)
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
        "command_token": str(uuid4()),
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
    customer_scope: str = "company",
    customer_ids: tuple[UUID, ...] = (),
    rule_id: UUID | None = None,
) -> dict[str, object]:
    selected_customers = tuple(
        match
        for customer_id in customer_ids
        if (match := customer_search.get_customer_match(db, customer_id)) is not None
    )
    return {
        **_base_context(request, db),
        "error": error,
        "name": name,
        "service_team_id": service_team_id,
        "service_teams": service_team_lifecycle.list_active_team_options(db),
        "customer_scope": customer_scope,
        "selected_customers": selected_customers,
        "rule_id": rule_id,
        "command_token": str(uuid4()),
    }


def _customer_selection(
    db: Session, *, customer_scope: str, customer_ids: list[str]
) -> tuple[UUID, ...]:
    if customer_scope == "company":
        return ()
    if customer_scope != "selected":
        raise ValueError(
            "Choose whether this rule applies to all or selected customers."
        )
    if not customer_ids:
        raise ValueError("Choose at least one customer for a customer-specific rule.")
    if len(customer_ids) > 100:
        raise ValueError("Select no more than 100 customers in one rule.")
    try:
        selected = tuple(UUID(item) for item in customer_ids)
    except (TypeError, ValueError) as exc:
        raise ValueError("One or more selected customers are invalid.") from exc
    if len(set(selected)) != len(selected):
        raise ValueError("A customer can only be selected once.")
    active = tuple(
        customer_search.get_customer_match(db, customer_id, active_only=True)
        for customer_id in selected
    )
    if any(item is None for item in active):
        raise ValueError(
            "A selected customer is no longer active. Search and choose again."
        )
    return selected


def _preserved_customer_ids(customer_ids: list[str]) -> tuple[UUID, ...]:
    """Keep valid picker selections when a form needs a correction."""

    selected: list[UUID] = []
    for item in customer_ids[:100]:
        try:
            customer_id = UUID(item)
        except (TypeError, ValueError):
            continue
        if customer_id not in selected:
            selected.append(customer_id)
    return tuple(selected)


def _ticket_assignment_conditions(
    customer_ids: tuple[UUID, ...],
) -> tuple[automation_rules.AutomationCondition, ...]:
    conditions = [
        automation_rules.AutomationCondition(
            field_key="priority",
            operator=automation_rules.AutomationOperator.equals,
            value="urgent",
        )
    ]
    if customer_ids:
        conditions.append(
            automation_rules.AutomationCondition(
                field_key="customer_id",
                operator=automation_rules.AutomationOperator.in_values,
                value=customer_ids,
            )
        )
    return tuple(conditions)


def _automation_redirect(*, error: str | None = None) -> RedirectResponse:
    url = "/admin/automation"
    if error:
        url = f"{url}?error={quote(error)}"
    return RedirectResponse(url=url, status_code=303)


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
        can_update_rules=has_permission(auth, db, RULE_UPDATE_PERMISSION),
        can_publish_rules=has_permission(auth, db, RULE_PUBLISH_PERMISSION),
        can_operate_rules=has_permission(auth, db, RULE_OPERATE_PERMISSION),
        can_read_support_tickets=has_permission(auth, db, "support:ticket:read"),
        can_update_support_tickets=has_permission(auth, db, "support:ticket:update"),
    )
    return templates.TemplateResponse(
        "admin/automation/index.html",
        {
            **_base_context(request, db),
            **state,
            "page_error": request.query_params.get("error"),
        },
    )


@router.get(
    "/customers/search",
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission("support:ticket:read")),
    ],
)
def search_rule_customers(
    q: str = Query(min_length=2, max_length=120), db: Session = Depends(get_db)
):
    """Return a bounded customer picker result for the draft form."""

    return JSONResponse(
        jsonable_encoder(customer_search.search_response(db, q, limit=20))
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
    customer_scope: str = Form(default="company"),
    customer_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(RULE_CREATE_PERMISSION)),
):
    """Save a fixed urgent-ticket assignment as a draft only."""

    try:
        selected_customer_ids = _customer_selection(
            db, customer_scope=customer_scope, customer_ids=customer_ids
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "admin/automation/ticket_assignment_draft.html",
            _pilot_form_context(
                request,
                db,
                error=str(exc),
                name=name,
                service_team_id=service_team_id,
                customer_scope=customer_scope,
                customer_ids=_preserved_customer_ids(customer_ids),
            ),
            status_code=400,
        )

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
                customer_scope=customer_scope,
                customer_ids=selected_customer_ids,
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
                conditions=_ticket_assignment_conditions(selected_customer_ids),
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
                customer_scope=customer_scope,
                customer_ids=selected_customer_ids,
            ),
            status_code=400,
        )
    return _automation_redirect()


@router.get(
    "/rules/{rule_id}/edit",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(RULE_UPDATE_PERMISSION)),
        Depends(require_permission("support:ticket:read")),
        Depends(require_permission("support:ticket:update")),
    ],
)
def edit_ticket_assignment_draft(
    rule_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        state = automation_rules.get_rule_editor_state(
            db,
            automation_rules.GetAutomationRuleEditorQuery(
                tenant_id=web_automation_center.OPERATOR_TENANT_ID,
                rule_id=rule_id,
            ),
        )
    except DomainError as exc:
        return _automation_redirect(error=str(exc))
    if (
        state.trigger_key != _PILOT_TRIGGER
        or state.service_team_id is None
        or state.status is automation_rules.AutomationRuleStatus.retired
    ):
        return _automation_redirect(error="This rule cannot be edited from this form.")
    return templates.TemplateResponse(
        "admin/automation/ticket_assignment_draft.html",
        _pilot_form_context(
            request,
            db,
            name=state.name,
            service_team_id=str(state.service_team_id),
            customer_scope="selected" if state.customer_ids else "company",
            customer_ids=state.customer_ids,
            rule_id=rule_id,
        ),
    )


@router.post(
    "/rules/{rule_id}/draft",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission("support:ticket:read")),
        Depends(require_permission("support:ticket:update")),
    ],
)
def replace_ticket_assignment_draft(
    rule_id: UUID,
    request: Request,
    service_team_id: str = Form(...),
    customer_scope: str = Form(default="company"),
    customer_ids: list[str] = Form(default=[]),
    command_token: str = Form(...),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(RULE_UPDATE_PERMISSION)),
):
    try:
        selected_customer_ids = _customer_selection(
            db, customer_scope=customer_scope, customer_ids=customer_ids
        )
        token = UUID(command_token)
    except (ValueError, TypeError) as exc:
        return templates.TemplateResponse(
            "admin/automation/ticket_assignment_draft.html",
            _pilot_form_context(
                request,
                db,
                error=str(exc) or "The form is no longer valid. Reload and try again.",
                service_team_id=service_team_id,
                customer_scope=customer_scope,
                customer_ids=_preserved_customer_ids(customer_ids),
                rule_id=rule_id,
            ),
            status_code=400,
        )
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
                service_team_id=service_team_id,
                customer_scope=customer_scope,
                customer_ids=selected_customer_ids,
                rule_id=rule_id,
            ),
            status_code=400,
        )
    try:
        db_session_adapter.release_read_transaction(db)
        automation_rules.replace_draft(
            db,
            automation_rules.ReplaceAutomationRuleDraftCommand(
                tenant_id=web_automation_center.OPERATOR_TENANT_ID,
                rule_id=rule_id,
                conditions=_ticket_assignment_conditions(selected_customer_ids),
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
                    scope="automation:rule:update",
                    reason="Administrator saved a ticket-assignment rule draft revision",
                    idempotency_key=f"automation-ticket-assignment-edit:{rule_id}:{token}",
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
                service_team_id=service_team_id,
                customer_scope=customer_scope,
                customer_ids=selected_customer_ids,
                rule_id=rule_id,
            ),
            status_code=400,
        )
    return _automation_redirect()


@router.post(
    "/rules/{rule_id}/publish",
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(RULE_PUBLISH_PERMISSION)),
    ],
)
def publish_automation_rule(
    rule_id: UUID,
    request: Request,
    command_token: str = Form(...),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(RULE_PUBLISH_PERMISSION)),
):
    try:
        token = UUID(command_token)
        db_session_adapter.release_read_transaction(db)
        automation_rules.publish_rule(
            db,
            automation_rules.PublishAutomationRuleCommand(
                tenant_id=web_automation_center.OPERATOR_TENANT_ID,
                rule_id=rule_id,
                permission_keys=frozenset(auth.get("permission_keys") or ()),
                context=CommandContext.system(
                    actor=_actor(request),
                    scope="automation:rule:publish",
                    reason="Administrator activated an automation rule version",
                    idempotency_key=f"automation-rule-publish:{rule_id}:{token}",
                ),
            ),
        )
    except (DomainError, ValueError) as exc:
        return _automation_redirect(error=str(exc))
    return _automation_redirect()


def _change_rule_status(
    *,
    rule_id: UUID,
    request: Request,
    db: Session,
    auth: dict,
    operation: automation_rules.AutomationRuleOperation,
    command_token: str,
) -> RedirectResponse:
    try:
        token = UUID(command_token)
        db_session_adapter.release_read_transaction(db)
        automation_rules.change_rule_status(
            db,
            automation_rules.ChangeAutomationRuleStatusCommand(
                tenant_id=web_automation_center.OPERATOR_TENANT_ID,
                rule_id=rule_id,
                operation=operation,
                permission_keys=frozenset(auth.get("permission_keys") or ()),
                context=CommandContext.system(
                    actor=_actor(request),
                    scope="automation:rule:operate",
                    reason=f"Administrator {operation.value}d an automation rule",
                    idempotency_key=(
                        f"automation-rule-{operation.value}:{rule_id}:{token}"
                    ),
                ),
            ),
        )
    except (DomainError, ValueError) as exc:
        return _automation_redirect(error=str(exc))
    return _automation_redirect()


@router.post(
    "/rules/{rule_id}/pause",
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(RULE_OPERATE_PERMISSION)),
    ],
)
def pause_automation_rule(
    rule_id: UUID,
    request: Request,
    command_token: str = Form(...),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(RULE_OPERATE_PERMISSION)),
):
    return _change_rule_status(
        rule_id=rule_id,
        request=request,
        db=db,
        auth=auth,
        operation=automation_rules.AutomationRuleOperation.pause,
        command_token=command_token,
    )


@router.post(
    "/rules/{rule_id}/resume",
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(RULE_OPERATE_PERMISSION)),
    ],
)
def resume_automation_rule(
    rule_id: UUID,
    request: Request,
    command_token: str = Form(...),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(RULE_OPERATE_PERMISSION)),
):
    return _change_rule_status(
        rule_id=rule_id,
        request=request,
        db=db,
        auth=auth,
        operation=automation_rules.AutomationRuleOperation.resume,
        command_token=command_token,
    )


__all__ = ["router"]
