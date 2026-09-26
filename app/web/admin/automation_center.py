"""Permission-gated Automation Center admin hub."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import (
    automation_capabilities,
    automation_rules,
    customer_search,
    service_team_lifecycle,
    web_automation_center,
)
from app.services.auth_dependencies import has_permission, require_permission
from app.services.automation_contracts import (
    AutomationConditionField,
    AutomationOperator,
    AutomationValueType,
)
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

_DEFAULT_TRIGGER = "support.ticket.created"
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


def _automation_redirect(*, error: str | None = None) -> RedirectResponse:
    url = "/admin/automation"
    if error:
        url = f"{url}?error={quote(error)}"
    return RedirectResponse(url=url, status_code=303)


def _generic_form_context(
    request: Request,
    db: Session,
    *,
    error: str | None = None,
    name: str = "",
    trigger_key: str = _DEFAULT_TRIGGER,
    customer_scope: str = "company",
    customer_ids: tuple[UUID, ...] = (),
    conditions: tuple[dict[str, object], ...] = (),
    actions: tuple[dict[str, object], ...] = (),
    rule_id: UUID | None = None,
    permission_keys: frozenset[str] = frozenset(),
) -> dict[str, object]:
    manifests = automation_capabilities.registered_module_manifests()
    authorized = "*" in permission_keys
    triggers = tuple(
        trigger
        for manifest in manifests
        for trigger in manifest.triggers
        if authorized or trigger.author_permission in permission_keys
    )
    selected_trigger = next(
        (item for item in triggers if item.key == trigger_key),
        triggers[0] if triggers else None,
    )
    builder_options: dict[str, object] = {}
    for item in triggers:
        compatible_actions = tuple(
            action
            for manifest in manifests
            for action in manifest.actions
            if action.entity_type == item.entity_type
            and (authorized or action.author_permission in permission_keys)
        )
        builder_options[item.key] = {
            "supports_customer_scope": any(
                field.key == "customer_id" for field in item.fields
            ),
            "fields": [
                {
                    "key": field.key,
                    "label": field.label,
                    "value_type": field.value_type.value,
                    "operators": [operator.value for operator in field.operators],
                    "enum_values": list(field.enum_values),
                }
                for field in item.fields
                if field.key != "customer_id"
            ],
            "actions": [
                {
                    "key": action.key,
                    "label": action.label,
                    "inputs": [
                        {
                            "key": value.key,
                            "label": value.label,
                            "value_type": value.value_type.value,
                            "required": value.required,
                            "enum_values": list(value.enum_values),
                        }
                        for value in action.inputs
                    ],
                }
                for action in compatible_actions
            ],
        }
    team_options = tuple(
        {"key": str(team_id), "label": label}
        for team_id, label in service_team_lifecycle.list_active_team_options(db)
    )
    return {
        **_base_context(request, db),
        "error": error,
        "name": name,
        "trigger_key": selected_trigger.key if selected_trigger else trigger_key,
        "triggers": triggers,
        "condition_fields": tuple(
            item
            for item in (selected_trigger.fields if selected_trigger else ())
            if item.key != "customer_id"
        ),
        "supports_customer_scope": bool(
            selected_trigger
            and any(field.key == "customer_id" for field in selected_trigger.fields)
        ),
        "service_teams": team_options,
        "builder_options": builder_options,
        "customer_scope": customer_scope,
        "selected_customers": tuple(
            match
            for customer_id in customer_ids
            if (match := customer_search.get_customer_match(db, customer_id))
            is not None
        ),
        "initial_conditions": jsonable_encoder(conditions),
        "initial_actions": jsonable_encoder(actions),
        "rule_id": rule_id,
        "command_token": str(uuid4()),
    }


def _form_scalar(field: AutomationConditionField, raw: object) -> object:
    if raw is None or raw == "":
        raise ValueError(f"Enter a value for {field.label}.")
    value = str(raw)
    if field.value_type in {AutomationValueType.string, AutomationValueType.enum}:
        return value
    if field.value_type is AutomationValueType.integer:
        return int(value)
    if field.value_type is AutomationValueType.decimal:
        return Decimal(value)
    if field.value_type is AutomationValueType.boolean:
        normalized = value.casefold()
        if normalized not in {"true", "false"}:
            raise ValueError("Choose a valid yes or no value.")
        return normalized == "true"
    if field.value_type is AutomationValueType.uuid:
        return UUID(value)
    if field.value_type is AutomationValueType.date:
        return date.fromisoformat(value)
    if field.value_type is AutomationValueType.datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("Choose a date and time with a time zone.")
        return parsed
    raise ValueError("This option has an unsupported value type.")


def _form_definition(
    *,
    trigger_key: str,
    conditions_json: str,
    actions_json: str,
    customer_ids: tuple[UUID, ...],
) -> tuple[
    tuple[automation_rules.AutomationCondition, ...],
    tuple[automation_rules.AutomationActionStep, ...],
]:
    raw_conditions = json.loads(conditions_json)
    raw_actions = json.loads(actions_json)
    if not isinstance(raw_conditions, list) or not isinstance(raw_actions, list):
        raise ValueError("Review the conditions and actions and try again.")
    trigger = automation_capabilities.trigger_capability(trigger_key)
    fields = {item.key: item for item in trigger.fields}
    if customer_ids and "customer_id" not in fields:
        raise ValueError("This trigger does not support selecting customers.")
    conditions: list[automation_rules.AutomationCondition] = []
    for item in raw_conditions:
        if not isinstance(item, dict):
            raise ValueError("A condition is not valid.")
        field = fields.get(str(item.get("field_key") or ""))
        if field is None or field.key == "customer_id":
            raise ValueError("Choose a condition field provided by the app.")
        operator = AutomationOperator(str(item.get("operator") or ""))
        raw_value = item.get("value")
        if operator in {AutomationOperator.is_empty, AutomationOperator.is_not_empty}:
            value = None
        elif operator in {
            AutomationOperator.in_values,
            AutomationOperator.not_in_values,
        }:
            if isinstance(raw_value, list):
                raw_values = tuple(
                    str(part).strip() for part in raw_value if str(part).strip()
                )
            elif isinstance(raw_value, str):
                raw_values = tuple(
                    part.strip() for part in raw_value.split(",") if part.strip()
                )
            else:
                raw_values = ()
            if not raw_values:
                raise ValueError(f"Enter one or more values for {field.label}.")
            value = tuple(_form_scalar(field, part.strip()) for part in raw_values)
        else:
            value = _form_scalar(field, raw_value)
        conditions.append(
            automation_rules.AutomationCondition(field.key, operator, value)
        )
    if customer_ids:
        conditions.append(
            automation_rules.AutomationCondition(
                field_key="customer_id",
                operator=AutomationOperator.in_values,
                value=customer_ids,
            )
        )
    actions: list[automation_rules.AutomationActionStep] = []
    for item in raw_actions:
        if not isinstance(item, dict) or not isinstance(item.get("inputs"), dict):
            raise ValueError("An action is not valid.")
        capability = automation_capabilities.action_capability(
            str(item.get("action_key") or "")
        )
        declared = {value.key: value for value in capability.inputs}
        unknown_inputs = set(item["inputs"]) - set(declared)
        if unknown_inputs:
            raise ValueError("An action includes an input the app does not support.")
        parsed_values: list[automation_rules.AutomationActionValue] = []
        for key, raw_value in item["inputs"].items():
            definition = declared[str(key)]
            if raw_value in (None, "") and not definition.required:
                continue
            parsed_values.append(
                automation_rules.AutomationActionValue(
                    key=str(key),
                    value=_form_scalar(
                        AutomationConditionField(
                            key=definition.key,
                            label=definition.label,
                            value_type=definition.value_type,
                            operators=(AutomationOperator.equals,),
                            enum_values=definition.enum_values,
                        ),
                        raw_value,
                    ),
                )
            )
        values = tuple(parsed_values)
        actions.append(automation_rules.AutomationActionStep(capability.key, values))
    return tuple(conditions), tuple(actions)


def _safe_json_list(raw: str) -> tuple[dict[str, object], ...]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


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
        permission_keys=frozenset(auth.get("permission_keys") or ()),
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
    "/rules/new",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(RULE_CREATE_PERMISSION)),
    ],
)
def new_automation_rule(
    request: Request,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("automation:hub:read")),
):
    """Render a rule builder from approved runtime capabilities."""

    return templates.TemplateResponse(
        "admin/automation/rule_builder.html",
        _generic_form_context(
            request,
            db,
            permission_keys=frozenset(auth.get("permission_keys") or ()),
        ),
    )


@router.post(
    "/rules",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("automation:hub:read")),
    ],
)
def create_automation_rule_draft(
    request: Request,
    name: str = Form(...),
    trigger_key: str = Form(...),
    conditions_json: str = Form(default="[]"),
    actions_json: str = Form(default="[]"),
    customer_scope: str = Form(default="company"),
    customer_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(RULE_CREATE_PERMISSION)),
):
    """Save an app-approved combination of conditions and actions as a draft."""

    try:
        selected_customer_ids = _customer_selection(
            db, customer_scope=customer_scope, customer_ids=customer_ids
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "admin/automation/rule_builder.html",
            _generic_form_context(
                request,
                db,
                error=str(exc),
                name=name,
                customer_scope=customer_scope,
                customer_ids=_preserved_customer_ids(customer_ids),
                permission_keys=frozenset(auth.get("permission_keys") or ()),
            ),
            status_code=400,
        )

    try:
        conditions, actions = _form_definition(
            trigger_key=trigger_key,
            conditions_json=conditions_json,
            actions_json=actions_json,
            customer_ids=selected_customer_ids,
        )
        key = "automation.rule." + _KEY_WORDS.sub("_", name.strip().casefold()).strip(
            "_"
        )[:104].rstrip("_")
        if key == "automation.rule.":
            raise ValueError("Rule name is required.")
        db_session_adapter.release_read_transaction(db)
        automation_rules.create_rule(
            db,
            automation_rules.CreateAutomationRuleCommand(
                tenant_id=web_automation_center.OPERATOR_TENANT_ID,
                key=key,
                name=name,
                description=None,
                trigger_key=trigger_key,
                conditions=conditions,
                actions=actions,
                permission_keys=frozenset(auth.get("permission_keys") or ()),
                context=CommandContext.system(
                    actor=_actor(request),
                    scope="automation:rule:create",
                    reason="Administrator saved an automation rule draft",
                    idempotency_key=f"automation-rule-draft:{key}",
                ),
            ),
        )
    except (DomainError, ValueError) as exc:
        return templates.TemplateResponse(
            "admin/automation/rule_builder.html",
            _generic_form_context(
                request,
                db,
                error=str(exc),
                name=name,
                trigger_key=trigger_key,
                customer_scope=customer_scope,
                customer_ids=selected_customer_ids,
                conditions=_safe_json_list(conditions_json),
                actions=_safe_json_list(actions_json),
                permission_keys=frozenset(auth.get("permission_keys") or ()),
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
    ],
)
def edit_automation_rule_draft(
    rule_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("automation:hub:read")),
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
    if state.status is automation_rules.AutomationRuleStatus.retired:
        return _automation_redirect(error="This rule cannot be edited from this form.")
    permission_keys = frozenset(auth.get("permission_keys") or ())
    required_permissions = {
        automation_capabilities.trigger_capability(state.trigger_key).author_permission,
        *(
            automation_capabilities.action_capability(item.action_key).author_permission
            for item in state.actions
        ),
    }
    if "*" not in permission_keys and not required_permissions.issubset(
        permission_keys
    ):
        return _automation_redirect(
            error="Your account is missing a permission required to edit this rule."
        )
    conditions = tuple(
        {
            "field_key": item.field_key,
            "operator": item.operator.value,
            "value": item.value,
        }
        for item in state.conditions
        if item.field_key != "customer_id"
    )
    actions = tuple(
        {
            "action_key": step.action_key,
            "inputs": {item.key: item.value for item in step.inputs},
        }
        for step in state.actions
    )
    return templates.TemplateResponse(
        "admin/automation/rule_builder.html",
        _generic_form_context(
            request,
            db,
            name=state.name,
            trigger_key=state.trigger_key,
            customer_scope="selected" if state.customer_ids else "company",
            customer_ids=state.customer_ids,
            conditions=conditions,
            actions=actions,
            rule_id=rule_id,
            permission_keys=permission_keys,
        ),
    )


@router.post(
    "/rules/{rule_id}/draft",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("automation:hub:read")),
    ],
)
def replace_automation_rule_draft(
    rule_id: UUID,
    request: Request,
    trigger_key: str = Form(...),
    conditions_json: str = Form(default="[]"),
    actions_json: str = Form(default="[]"),
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
        conditions, actions = _form_definition(
            trigger_key=trigger_key,
            conditions_json=conditions_json,
            actions_json=actions_json,
            customer_ids=selected_customer_ids,
        )
        token = UUID(command_token)
    except (ValueError, TypeError) as exc:
        return templates.TemplateResponse(
            "admin/automation/rule_builder.html",
            _generic_form_context(
                request,
                db,
                error=str(exc) or "The form is no longer valid. Reload and try again.",
                trigger_key=trigger_key,
                customer_scope=customer_scope,
                customer_ids=_preserved_customer_ids(customer_ids),
                rule_id=rule_id,
                permission_keys=frozenset(auth.get("permission_keys") or ()),
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
                conditions=conditions,
                actions=actions,
                permission_keys=frozenset(auth.get("permission_keys") or ()),
                context=CommandContext.system(
                    actor=_actor(request),
                    scope="automation:rule:update",
                    reason="Administrator saved an automation rule draft revision",
                    idempotency_key=f"automation-rule-edit:{rule_id}:{token}",
                ),
            ),
        )
    except (DomainError, ValueError) as exc:
        return templates.TemplateResponse(
            "admin/automation/rule_builder.html",
            _generic_form_context(
                request,
                db,
                error=str(exc),
                trigger_key=trigger_key,
                customer_scope=customer_scope,
                customer_ids=selected_customer_ids,
                conditions=_safe_json_list(conditions_json),
                actions=_safe_json_list(actions_json),
                rule_id=rule_id,
                permission_keys=frozenset(auth.get("permission_keys") or ()),
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
