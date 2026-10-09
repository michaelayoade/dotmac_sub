"""Permission-gated Automation Center admin hub."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.automation_scripts import (
    AutomationScriptKind,
    AutomationScriptLanguage,
)
from app.services import (
    automation_capabilities,
    automation_condition_lookups,
    automation_rules,
    automation_runtime,
    automation_script_runtime,
    automation_scripts,
    customer_search,
    event_replay_evidence,
    service_team_lifecycle,
    web_automation_center,
)
from app.services.auth_dependencies import (
    has_permission,
    require_permission,
    require_user_auth,
)
from app.services.automation_contracts import (
    AutomationConditionField,
    AutomationLookupKey,
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
from app.services.automation_runtime import (
    RUN_READ_PERMISSION,
    RUN_REDRIVE_PERMISSION,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.events.types import EventType
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import CommandContext

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/automation", tags=["web-admin-automation"])

# The builder is module-first; an empty initial key lets its capability list
# choose the first authorized module instead of making Tickets the implicit
# owner of the central authoring entry point.
_DEFAULT_TRIGGER = ""
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


def _automation_redirect(
    *,
    path: str = "/admin/automation",
    error: str | None = None,
    notice: str | None = None,
) -> RedirectResponse:
    safe_path = path if path.startswith("/admin/automation") else "/admin/automation"
    url = safe_path
    if error:
        url = f"{url}?error={quote(error)}"
    elif notice:
        url = f"{url}?notice={quote(notice)}"
    return RedirectResponse(url=url, status_code=303)


def _generic_form_context(
    request: Request,
    db: Session,
    *,
    error: str | None = None,
    name: str = "",
    trigger_key: str = _DEFAULT_TRIGGER,
    trigger_keys: tuple[str, ...] = (),
    customer_scope: str = "company",
    customer_ids: tuple[UUID, ...] = (),
    conditions: Mapping[str, object] | tuple[Mapping[str, object], ...] = (),
    actions: tuple[Mapping[str, object], ...] = (),
    schedule: Mapping[str, object] | None = None,
    rule_id: UUID | None = None,
    permission_keys: frozenset[str] = frozenset(),
) -> dict[str, object]:
    manifests = automation_capabilities.registered_module_manifests()
    authorized = "*" in permission_keys
    triggers = tuple(
        trigger
        for manifest in manifests
        for trigger in manifest.triggers
        if trigger.runtime_enabled
        if authorized or trigger.author_permission in permission_keys
    )
    module_options = tuple(
        {
            "key": manifest.module_key,
            "label": manifest.label,
            "trigger_keys": [
                trigger.key
                for trigger in manifest.triggers
                if trigger.runtime_enabled
                if authorized or trigger.author_permission in permission_keys
            ],
        }
        for manifest in manifests
        if any(
            trigger.runtime_enabled
            and (authorized or trigger.author_permission in permission_keys)
            for trigger in manifest.triggers
        )
    )
    selected_keys = tuple(dict.fromkeys(trigger_keys or (trigger_key,)))
    selected_triggers = tuple(item for item in triggers if item.key in selected_keys)
    selected_trigger = next(
        (item for item in selected_triggers if item.key == trigger_key),
        triggers[0] if triggers else None,
    )
    selected_keys = tuple(item.key for item in selected_triggers) or (
        (selected_trigger.key,) if selected_trigger else ()
    )
    union_fields = {
        field.key: field
        for item in selected_triggers
        for field in item.fields
        if field.key != "customer_id"
    }
    builder_options: dict[str, object] = {}
    for item in triggers:
        compatible_actions = tuple(
            action
            for manifest in manifests
            for action in manifest.actions
            if automation_capabilities.action_applies_to_entity(
                action, item.entity_type
            )
            and action.authoring_enabled
            and action.runtime_enabled
            and (authorized or action.author_permission in permission_keys)
        )
        builder_options[item.key] = {
            "scheduled": item.scheduled,
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
                    "lookup_key": field.lookup_key.value if field.lookup_key else None,
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
        "trigger_keys": selected_keys,
        "triggers": triggers,
        "module_options": module_options,
        "condition_fields": tuple(union_fields.values()),
        "supports_customer_scope": bool(
            any(
                field.key == "customer_id"
                for item in selected_triggers
                for field in item.fields
            )
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
        "initial_schedule": jsonable_encoder(schedule or {}),
        "rule_id": rule_id,
        "command_token": str(uuid4()),
    }


def _script_form_context(
    request: Request,
    db: Session,
    *,
    kind: AutomationScriptKind,
    error: str | None = None,
    name: str = "",
    key: str = "",
    description: str = "",
    target_type: str = "",
    event_name: str = "",
    source_code: str = "",
    script_id: UUID | None = None,
    source_version: int | None = None,
    return_to: str | None = None,
) -> dict[str, object]:
    target_groups = tuple(
        {
            "module_key": manifest.module_key,
            "module_label": manifest.label,
            "targets": tuple(
                {
                    "key": target.key,
                    "label": target.label,
                    "entity_type": target.entity_type,
                    "events": list(
                        target.client_events
                        if kind is AutomationScriptKind.client
                        else target.server_events
                    ),
                }
                for target in manifest.script_targets
                if (
                    target.client_events
                    if kind is AutomationScriptKind.client
                    else target.server_events
                )
            ),
        }
        for manifest in automation_capabilities.registered_module_manifests()
        if any(
            target.client_events
            if kind is AutomationScriptKind.client
            else target.server_events
            for target in manifest.script_targets
        )
    )
    targets = tuple(
        {
            "key": target.key,
            "label": target.label,
            "entity_type": target.entity_type,
            "events": list(
                target.client_events
                if kind is AutomationScriptKind.client
                else target.server_events
            ),
        }
        for manifest in automation_capabilities.registered_module_manifests()
        for target in manifest.script_targets
        if (
            target.client_events
            if kind is AutomationScriptKind.client
            else target.server_events
        )
    )
    selected_target = target_type or (str(targets[0]["entity_type"]) if targets else "")
    selected_events: list[object] = []
    for item in targets:
        if item["entity_type"] == selected_target and isinstance(item["events"], list):
            selected_events = item["events"]
            break
    selected_event = event_name or (str(selected_events[0]) if selected_events else "")
    return {
        **_base_context(request, db),
        "error": error,
        "script_kind": kind.value,
        "script_kind_label": "Client script"
        if kind is AutomationScriptKind.client
        else "Server script",
        "script_targets": targets,
        "script_target_groups": target_groups,
        "name": name,
        "key": key,
        "description": description,
        "target_type": selected_target,
        "event_name": selected_event,
        "source_code": source_code,
        "script_id": script_id,
        "edit_mode": script_id is not None,
        "source_version": source_version,
        "script_action": (
            f"/admin/automation/scripts/{script_id}/versions"
            if script_id is not None
            else "/admin/automation/scripts"
        ),
        "command_token": str(uuid4()),
        "return_to": return_to
        or (
            "/admin/automation/client-scripts/manage"
            if kind is AutomationScriptKind.client
            else "/admin/automation/server-scripts"
        ),
    }


def _form_scalar(
    field: AutomationConditionField, raw: object
) -> automation_rules.AutomationScalar:
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
    trigger_keys: tuple[str, ...] = (),
    conditions_json: str,
    actions_json: str,
    customer_ids: tuple[UUID, ...],
) -> tuple[
    tuple[automation_rules.AutomationCondition, ...]
    | automation_rules.AutomationConditionGroup,
    tuple[automation_rules.AutomationActionStep, ...],
]:
    raw_conditions = json.loads(conditions_json)
    raw_actions = json.loads(actions_json)
    if not isinstance(raw_conditions, (list, dict)) or not isinstance(
        raw_actions, list
    ):
        raise ValueError("Review the conditions and actions and try again.")
    selected_keys = tuple(dict.fromkeys((trigger_key, *trigger_keys)))
    triggers = tuple(
        automation_capabilities.trigger_capability(key) for key in selected_keys
    )
    fields = {item.key: item for trigger in triggers for item in trigger.fields}
    if customer_ids and "customer_id" not in fields:
        raise ValueError("This trigger does not support selecting customers.")

    def parse_condition(
        item: object,
    ) -> (
        automation_rules.AutomationCondition | automation_rules.AutomationConditionGroup
    ):
        if not isinstance(item, dict):
            raise ValueError("A condition is not valid.")
        if "field_key" not in item:
            try:
                group = automation_rules.AutomationConditionGroupOperator(
                    str(item.get("group") or "")
                )
            except ValueError as exc:
                raise ValueError("Choose a valid condition group.") from exc
            children = item.get("children")
            if not isinstance(children, list):
                raise ValueError("A condition group is not valid.")
            return automation_rules.AutomationConditionGroup(
                operator=group,
                children=tuple(parse_condition(child) for child in children),
            )
        field = fields.get(str(item.get("field_key") or ""))
        if field is None or field.key == "customer_id":
            raise ValueError("Choose a condition field provided by the app.")
        operator = AutomationOperator(str(item.get("operator") or ""))
        raw_value = item.get("value")
        value: (
            automation_rules.AutomationScalar
            | tuple[automation_rules.AutomationScalar, ...]
        )
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
        return automation_rules.AutomationCondition(field.key, operator, value)

    if isinstance(raw_conditions, list):
        parsed_conditions = tuple(parse_condition(item) for item in raw_conditions)
        if not all(
            isinstance(item, automation_rules.AutomationCondition)
            for item in parsed_conditions
        ):
            raise ValueError(
                "A top-level condition list must contain field conditions."
            )
        conditions: (
            tuple[automation_rules.AutomationCondition, ...]
            | automation_rules.AutomationConditionGroup
        ) = tuple(
            item
            for item in parsed_conditions
            if isinstance(item, automation_rules.AutomationCondition)
        )
    else:
        parsed_root = parse_condition(raw_conditions)
        if not isinstance(parsed_root, automation_rules.AutomationConditionGroup):
            raise ValueError("The root condition must be a group.")
        conditions = parsed_root
    if customer_ids:
        customer_condition = automation_rules.AutomationCondition(
            field_key="customer_id",
            operator=AutomationOperator.in_values,
            value=customer_ids,
        )
        if isinstance(conditions, tuple):
            conditions = (*conditions, customer_condition)
        elif (
            conditions.operator
            is automation_rules.AutomationConditionGroupOperator.and_
            and not conditions.children
        ):
            conditions = automation_rules.AutomationConditionGroup(
                operator=automation_rules.AutomationConditionGroupOperator.and_,
                children=(customer_condition,),
            )
        else:
            conditions = automation_rules.AutomationConditionGroup(
                operator=automation_rules.AutomationConditionGroupOperator.and_,
                children=(conditions, customer_condition),
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
    return conditions, tuple(actions)


def _safe_json_list(raw: str) -> tuple[Mapping[str, object], ...]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _safe_json_value(raw: str) -> object:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []


def _safe_json_conditions(raw: str) -> tuple[Mapping[str, object], ...]:
    value = _safe_json_value(raw)
    if isinstance(value, Mapping):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, Mapping))
    return ()


def _safe_json_schedule(raw: str) -> Mapping[str, object] | None:
    value = _safe_json_value(raw)
    return value if isinstance(value, Mapping) else None


def _form_schedule(raw: str) -> dict[str, object] | None:
    value = _safe_json_value(raw)
    if value in (None, {}, []):
        return None
    if not isinstance(value, dict):
        raise ValueError("Review the schedule and try again.")
    return {str(key): item for key, item in value.items()}


def _form_trigger_keys(primary: str, raw: str) -> tuple[str, ...]:
    value = _safe_json_value(raw)
    if not isinstance(value, list):
        return (primary,)
    keys = tuple(
        dict.fromkeys(str(item).strip() for item in value if str(item).strip())
    )
    return keys or (primary,)


def _editor_condition_value(value: object) -> Mapping[str, object] | None:
    if isinstance(value, automation_rules.AutomationCondition):
        if value.field_key == "customer_id":
            return None
        return {
            "field_key": value.field_key,
            "operator": value.operator.value,
            "value": value.value,
        }
    if isinstance(value, automation_rules.AutomationConditionGroup):
        return {
            "group": value.operator.value,
            "children": [
                child_value
                for child in value.children
                if (child_value := _editor_condition_value(child)) is not None
            ],
        }
    if isinstance(value, tuple):
        return {
            "group": "and",
            "children": [
                child_value
                for child in value
                if (child_value := _editor_condition_value(child)) is not None
            ],
        }
    return None


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
        can_read_scripts=has_permission(
            auth, db, automation_scripts.SCRIPT_READ_PERMISSION
        ),
        can_create_scripts=has_permission(
            auth, db, automation_scripts.SCRIPT_CREATE_PERMISSION
        ),
        can_publish_scripts=has_permission(
            auth, db, automation_scripts.SCRIPT_PUBLISH_PERMISSION
        ),
        can_update_scripts=has_permission(
            auth, db, automation_scripts.SCRIPT_UPDATE_PERMISSION
        ),
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


def _automation_module_options() -> tuple[dict[str, str], ...]:
    return tuple(
        {"key": manifest.module_key, "label": manifest.label}
        for manifest in automation_capabilities.registered_module_manifests()
    )


@router.get(
    "/workflows",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(RULE_READ_PERMISSION)),
    ],
)
def automation_workflow_list(
    request: Request,
    q: str = Query(default="", max_length=160),
    status: str = Query(default="", max_length=24),
    module: str = Query(default="", max_length=120),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("automation:hub:read")),
):
    """List workflows as the focused operator workspace."""

    can_update = has_permission(auth, db, RULE_UPDATE_PERMISSION)
    can_publish = has_permission(auth, db, RULE_PUBLISH_PERMISSION)
    can_operate = has_permission(auth, db, RULE_OPERATE_PERMISSION)
    can_create = has_permission(auth, db, RULE_CREATE_PERMISSION)
    rules = automation_rules.list_rules(
        db,
        automation_rules.ListAutomationRulesQuery(
            tenant_id=OPERATOR_TENANT_ID,
            include_retired=False,
        ),
    )
    trigger_modules = {
        trigger.key: manifest.module_key
        for manifest in automation_capabilities.registered_module_manifests()
        for trigger in manifest.triggers
    }
    search = q.strip().casefold()
    filtered_rules = tuple(
        rule
        for rule in rules
        if (
            not search
            or search in rule.name.casefold()
            or search in rule.trigger_key.casefold()
        )
        and (not status or rule.status.value == status)
        and (not module or trigger_modules.get(rule.trigger_key) == module)
    )
    return templates.TemplateResponse(
        "admin/automation/workflows.html",
        {
            **_base_context(request, db),
            "workflows": filtered_rules,
            "query": q,
            "selected_status": status,
            "selected_module": module,
            "statuses": tuple(automation_rules.AutomationRuleStatus),
            "modules": _automation_module_options(),
            "can_create_rules": can_create,
            "can_update_rules": can_update,
            "can_publish_rules": can_publish,
            "can_operate_rules": can_operate,
            "page_notice": request.query_params.get("notice"),
            "page_error": request.query_params.get("error"),
        },
    )


def _script_workspace(
    request: Request,
    db: Session,
    *,
    kind: AutomationScriptKind,
    q: str,
    target_type: str,
    event_name: str,
    status: str,
    updated_from: date | None,
    updated_to: date | None,
    auth: dict,
):
    """Render one mechanism-specific script list without mixing script kinds."""

    scripts = automation_scripts.list_scripts(db, tenant_id=OPERATOR_TENANT_ID)
    search = q.strip().casefold()
    filtered_scripts = tuple(
        script
        for script in scripts
        if script.kind is kind
        and (
            not search
            or search in script.name.casefold()
            or search in script.key.casefold()
        )
        and (not target_type or script.target_type == target_type)
        and (not event_name or script.event_name == event_name)
        and (not status or script.status.value == status)
        and (updated_from is None or script.updated_at.date() >= updated_from)
        and (updated_to is None or script.updated_at.date() <= updated_to)
    )
    target_groups = tuple(
        {
            "module_label": manifest.label,
            "targets": tuple(
                {
                    "key": target.entity_type,
                    "label": target.label,
                    "events": list(
                        target.client_events
                        if kind is AutomationScriptKind.client
                        else target.server_events
                    ),
                }
                for target in manifest.script_targets
                if (
                    target.client_events
                    if kind is AutomationScriptKind.client
                    else target.server_events
                )
            ),
        }
        for manifest in automation_capabilities.registered_module_manifests()
        if any(
            target.client_events
            if kind is AutomationScriptKind.client
            else target.server_events
            for target in manifest.script_targets
        )
    )
    return templates.TemplateResponse(
        "admin/automation/script_list.html",
        {
            **_base_context(request, db),
            "scripts": filtered_scripts,
            "script_kind": kind.value,
            "script_kind_label": "Client scripts"
            if kind is AutomationScriptKind.client
            else "Server scripts",
            "script_description": (
                "JavaScript that runs in declared browser form events through the restricted client API."
                if kind is AutomationScriptKind.client
                else "JavaScript drafts dispatched through the isolated runtime and typed owner actions."
            ),
            "query": q,
            "selected_target": target_type,
            "selected_event": event_name,
            "selected_status": status,
            "updated_from": updated_from.isoformat() if updated_from else "",
            "updated_to": updated_to.isoformat() if updated_to else "",
            "statuses": tuple(automation_scripts.AutomationScriptStatus),
            "target_groups": target_groups,
            "can_create_scripts": has_permission(
                auth, db, automation_scripts.SCRIPT_CREATE_PERMISSION
            ),
            "can_publish_scripts": has_permission(
                auth, db, automation_scripts.SCRIPT_PUBLISH_PERMISSION
            ),
            "can_update_scripts": has_permission(
                auth, db, automation_scripts.SCRIPT_UPDATE_PERMISSION
            ),
            "server_script_runtime_state": automation_script_runtime.runtime_state().value,
            "return_to": (
                "/admin/automation/client-scripts/manage"
                if kind is AutomationScriptKind.client
                else "/admin/automation/server-scripts"
            ),
            "page_notice": request.query_params.get("notice"),
            "page_error": request.query_params.get("error"),
        },
    )


@router.get(
    "/client-scripts/manage",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(automation_scripts.SCRIPT_READ_PERMISSION)),
    ],
)
def client_script_workspace(
    request: Request,
    q: str = Query(default="", max_length=160),
    target_type: str = Query(default="", max_length=120),
    event_name: str = Query(default="", max_length=160),
    status: str = Query(default="", max_length=24),
    updated_from: date | None = Query(default=None),
    updated_to: date | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("automation:hub:read")),
):
    return _script_workspace(
        request,
        db,
        kind=AutomationScriptKind.client,
        q=q,
        target_type=target_type,
        event_name=event_name,
        status=status,
        updated_from=updated_from,
        updated_to=updated_to,
        auth=auth,
    )


@router.get(
    "/server-scripts",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(automation_scripts.SCRIPT_READ_PERMISSION)),
    ],
)
def server_script_workspace(
    request: Request,
    q: str = Query(default="", max_length=160),
    target_type: str = Query(default="", max_length=120),
    event_name: str = Query(default="", max_length=160),
    status: str = Query(default="", max_length=24),
    updated_from: date | None = Query(default=None),
    updated_to: date | None = Query(default=None),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("automation:hub:read")),
):
    return _script_workspace(
        request,
        db,
        kind=AutomationScriptKind.server,
        q=q,
        target_type=target_type,
        event_name=event_name,
        status=status,
        updated_from=updated_from,
        updated_to=updated_to,
        auth=auth,
    )


@router.get(
    "/runs",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(RUN_READ_PERMISSION)),
    ],
)
def automation_run_history(
    request: Request,
    status: automation_runtime.AutomationRunStatus | None = Query(default=None),
    page: int = Query(default=1, ge=1, le=10000),
    per_page: int = Query(default=50, ge=1, le=100),
    sort: str | None = Query(default=None),
    sort_dir: str | None = Query(default=None, alias="dir"),
    db: Session = Depends(get_db),
):
    """List recent runs with optional status filtering."""

    with db_session_adapter.read_session() as read_db:
        page_data = automation_runtime.list_run_history(
            read_db,
            automation_runtime.GetAutomationRunHistoryQuery(
                tenant_id=web_automation_center.OPERATOR_TENANT_ID,
                status=status,
                page=page,
                per_page=per_page,
                sort_by=sort,
                sort_dir=sort_dir,
            ),
        )
    if page != page_data.page.page:
        return RedirectResponse(
            url=page_data.list_query.url("/admin/automation/runs"),
            status_code=303,
        )
    return templates.TemplateResponse(
        "admin/automation/run_history.html",
        {
            **_base_context(request, db),
            "runs": page_data.runs,
            "list_query": page_data.list_query,
            "page_meta": page_data.page,
            "previous_url": page_data.previous_url,
            "next_url": page_data.next_url,
            "selected_status": page_data.list_query.filter_value("status") or "",
            "selected_per_page": page_data.list_query.per_page,
            "statuses": tuple(automation_runtime.AutomationRunStatus),
        },
    )


@router.get(
    "/runs/{run_id}",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(RUN_READ_PERMISSION)),
    ],
)
def automation_run_detail(
    run_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("automation:hub:read")),
):
    """Show one run, its affected record, ordered steps, and retry history."""

    if not has_permission(auth, db, RUN_READ_PERMISSION):
        return RedirectResponse(url="/admin/automation", status_code=303)
    with db_session_adapter.read_session() as read_db:
        try:
            detail = automation_runtime.get_run_detail(
                read_db,
                automation_runtime.GetAutomationRunDetailQuery(
                    tenant_id=web_automation_center.OPERATOR_TENANT_ID,
                    run_id=run_id,
                ),
            )
        except DomainError:
            return RedirectResponse(
                url="/admin/automation?error=Run%20not%20found",
                status_code=303,
            )
    action_labels: dict[UUID, str] = {}
    for step in detail.steps:
        try:
            action_labels[step.step_id] = automation_capabilities.action_capability(
                step.action_key
            ).label
        except automation_capabilities.AutomationCapabilityError:
            action_labels[step.step_id] = step.action_key
    return templates.TemplateResponse(
        "admin/automation/run_detail.html",
        {
            **_base_context(request, db),
            "detail": detail,
            "action_labels": action_labels,
            "can_retry": (
                has_permission(auth, db, RUN_REDRIVE_PERMISSION)
                and detail.summary.status
                is automation_runtime.AutomationRunStatus.failed
            ),
            "target_href": (
                f"/admin/support/tickets/{detail.summary.target_id}"
                if detail.summary.target_type == "support.ticket"
                else None
            ),
            "page_error": request.query_params.get("error"),
            "page_notice": request.query_params.get("notice"),
        },
    )


@router.post(
    "/runs/{run_id}/retry",
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(RUN_REDRIVE_PERMISSION)),
    ],
)
def retry_automation_run(
    run_id: UUID,
    request: Request,
    command_token: str = Form(...),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("automation:hub:read")),
):
    """Continue a failed run from its first unfinished step."""

    if not has_permission(auth, db, RUN_REDRIVE_PERMISSION):
        return RedirectResponse(url="/admin/automation", status_code=303)
    detail_url = f"/admin/automation/runs/{run_id}"
    try:
        command_id = UUID(command_token)
    except (TypeError, ValueError):
        return RedirectResponse(
            url=f"{detail_url}?error=Retry%20request%20is%20invalid",
            status_code=303,
        )
    try:
        with db_session_adapter.read_session() as read_db:
            detail = automation_runtime.get_run_detail(
                read_db,
                automation_runtime.GetAutomationRunDetailQuery(
                    tenant_id=web_automation_center.OPERATOR_TENANT_ID,
                    run_id=run_id,
                ),
            )
            try:
                expected_event_type = EventType(detail.summary.event_type)
            except ValueError as exc:
                raise DomainError(
                    code="automation.execution.retry_event_type_invalid",
                    message="The original event type can no longer be retried.",
                ) from exc
            evidence = event_replay_evidence.get_durable_event_for_replay(
                read_db,
                event_replay_evidence.GetDurableEventForReplayQuery(
                    event_id=detail.summary.event_id,
                    expected_event_type=expected_event_type,
                ),
            )
        retry_context = CommandContext.system(
            actor=_actor(request),
            scope=RUN_REDRIVE_PERMISSION,
            reason="Administrator continued a failed automation run",
            command_id=command_id,
            correlation_id=run_id,
            idempotency_key=f"automation-run-retry:{run_id}:{command_id}",
        )
        with db_session_adapter.owner_command_session() as command_db:
            outcome = automation_runtime.retry_failed_run(
                command_db,
                automation_runtime.RetryFailedAutomationRunCommand(
                    tenant_id=web_automation_center.OPERATOR_TENANT_ID,
                    run_id=run_id,
                    event=evidence,
                    context=retry_context,
                ),
            )
    except DomainError as exc:
        return RedirectResponse(
            url=f"{detail_url}?error={quote(exc.message)}",
            status_code=303,
        )
    if outcome.retry_status is automation_runtime.AutomationRunRetryStatus.failed:
        return RedirectResponse(
            url=f"{detail_url}?error={quote(outcome.error_message or 'Retry did not complete')}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"{detail_url}?notice=Retry%20completed",
        status_code=303,
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
    "/condition-options/{lookup_key}",
    dependencies=[
        Depends(require_permission("automation:hub:read")),
    ],
)
def search_rule_condition_options(
    lookup_key: str,
    q: str = Query(default="", max_length=120),
    limit: int = Query(default=20, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Return lazy, canonical options for a declared condition lookup."""

    try:
        key = AutomationLookupKey(lookup_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail="Condition lookup not found"
        ) from exc
    options = automation_condition_lookups.lookup_options(db, key, q=q, limit=limit)
    return JSONResponse(jsonable_encoder({"items": options}))


@router.get("/client-scripts", response_class=JSONResponse)
def published_client_script_bundle(
    target_type: str = Query(min_length=1, max_length=120),
    event_name: str = Query(min_length=1, max_length=160),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_user_auth),
):
    """Return published client scripts for one authorized declared form event."""

    target = next(
        (
            item
            for manifest in automation_capabilities.registered_module_manifests()
            for item in manifest.script_targets
            if item.entity_type == target_type
        ),
        None,
    )
    if target is None or event_name not in target.client_events:
        raise HTTPException(status_code=404, detail="Client script target not found")
    if not has_permission(auth, db, target.read_permission):
        raise HTTPException(status_code=403, detail="Forbidden")
    scripts = automation_scripts.published_client_scripts(
        db,
        tenant_id=OPERATOR_TENANT_ID,
        target_type=target_type,
        event_name=event_name,
    )
    return JSONResponse(
        jsonable_encoder(
            {
                "target_type": target_type,
                "event_name": event_name,
                "scripts": scripts,
            }
        )
    )


@router.get(
    "/scripts/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("automation:hub:read"))],
)
def new_automation_script(
    request: Request,
    kind: str = Query(default=AutomationScriptKind.client.value),
    db: Session = Depends(get_db),
):
    """Render a typed client/server script draft form."""

    try:
        script_kind = AutomationScriptKind(kind)
    except ValueError:
        script_kind = AutomationScriptKind.client
    return templates.TemplateResponse(
        "admin/automation/script_builder.html",
        _script_form_context(request, db, kind=script_kind),
    )


@router.get(
    "/scripts/{script_id}",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(automation_scripts.SCRIPT_READ_PERMISSION)),
    ],
)
def automation_script_detail(
    script_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("automation:hub:read")),
):
    """Show the active and editable draft source for one script."""

    detail = automation_scripts.get_script(
        db, tenant_id=OPERATOR_TENANT_ID, script_id=script_id
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Script not found")
    return templates.TemplateResponse(
        "admin/automation/script_detail.html",
        {
            **_base_context(request, db),
            "detail": detail,
            "can_update_scripts": has_permission(
                auth, db, automation_scripts.SCRIPT_UPDATE_PERMISSION
            ),
            "can_publish_scripts": has_permission(
                auth, db, automation_scripts.SCRIPT_PUBLISH_PERMISSION
            ),
            "return_to": f"/admin/automation/scripts/{script_id}",
            "page_error": request.query_params.get("error"),
            "page_notice": request.query_params.get("notice"),
        },
    )


@router.get(
    "/scripts/{script_id}/edit",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(automation_scripts.SCRIPT_READ_PERMISSION)),
        Depends(require_permission(automation_scripts.SCRIPT_UPDATE_PERMISSION)),
    ],
)
def edit_automation_script(
    script_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    """Render an editable draft backed by a published script's immutable identity."""

    detail = automation_scripts.get_script(
        db, tenant_id=OPERATOR_TENANT_ID, script_id=script_id
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Script not found")
    return templates.TemplateResponse(
        "admin/automation/script_builder.html",
        _script_form_context(
            request,
            db,
            kind=detail.kind,
            name=detail.name,
            key=detail.key,
            description=detail.description or "",
            target_type=detail.target_type,
            event_name=detail.event_name,
            source_code=detail.draft_source_code
            if detail.draft_source_code is not None
            else detail.active_source_code or "",
            script_id=detail.script_id,
            source_version=detail.draft_version or detail.active_version,
            return_to=f"/admin/automation/scripts/{script_id}",
        ),
    )


@router.post(
    "/scripts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("automation:hub:read"))],
)
def create_automation_script_draft(
    request: Request,
    kind: str = Form(...),
    name: str = Form(...),
    key: str = Form(...),
    description: str = Form(default=""),
    target_type: str = Form(...),
    event_name: str = Form(...),
    source_code: str = Form(...),
    return_to: str = Form(default="/admin/automation"),
    db: Session = Depends(get_db),
    auth: dict = Depends(
        require_permission(automation_scripts.SCRIPT_CREATE_PERMISSION)
    ),
):
    """Persist a script draft through the script owner, never through the route."""

    try:
        script_kind = AutomationScriptKind(kind)
    except ValueError:
        return templates.TemplateResponse(
            "admin/automation/script_builder.html",
            _script_form_context(
                request,
                db,
                kind=AutomationScriptKind.client,
                error="Choose a supported script mechanism.",
                name=name,
                key=key,
                description=description,
                target_type=target_type,
                event_name=event_name,
                source_code=source_code,
            ),
            status_code=400,
        )
    try:
        outcome = automation_scripts.create_script(
            db,
            automation_scripts.CreateAutomationScriptCommand(
                tenant_id=OPERATOR_TENANT_ID,
                key=key,
                name=name,
                description=description,
                kind=script_kind,
                language=AutomationScriptLanguage.javascript,
                target_type=target_type,
                event_name=event_name,
                source_code=source_code,
                permission_keys=frozenset(auth.get("permission_keys") or ()),
                context=CommandContext.system(
                    actor=_actor(request),
                    scope="automation.script_definitions",
                    reason="Create Automation Center script draft",
                ),
            ),
        )
    except DomainError as exc:
        return templates.TemplateResponse(
            "admin/automation/script_builder.html",
            _script_form_context(
                request,
                db,
                kind=script_kind,
                error=exc.message,
                name=name,
                key=key,
                description=description,
                target_type=target_type,
                event_name=event_name,
                source_code=source_code,
            ),
            status_code=400,
        )
    return _automation_redirect(
        path=return_to,
        notice=f"Script draft {outcome.script_id} saved",
    )


@router.post(
    "/scripts/{script_id}/versions",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(automation_scripts.SCRIPT_READ_PERMISSION)),
        Depends(require_permission(automation_scripts.SCRIPT_UPDATE_PERMISSION)),
    ],
)
def update_automation_script_draft(
    script_id: UUID,
    request: Request,
    name: str = Form(...),
    description: str = Form(default=""),
    source_code: str = Form(...),
    command_token: str = Form(...),
    return_to: str = Form(default=""),
    db: Session = Depends(get_db),
    auth: dict = Depends(
        require_permission(automation_scripts.SCRIPT_UPDATE_PERMISSION)
    ),
):
    """Create or replace a draft while leaving the active version untouched."""

    detail = automation_scripts.get_script(
        db, tenant_id=OPERATOR_TENANT_ID, script_id=script_id
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Script not found")
    target = return_to or f"/admin/automation/scripts/{script_id}"
    try:
        token = UUID(command_token)
        db_session_adapter.release_read_transaction(db)
        outcome = automation_scripts.create_script_version(
            db,
            automation_scripts.CreateAutomationScriptVersionCommand(
                tenant_id=OPERATOR_TENANT_ID,
                script_id=script_id,
                name=name,
                description=description,
                source_code=source_code,
                permission_keys=frozenset(auth.get("permission_keys") or ()),
                context=CommandContext.system(
                    actor=_actor(request),
                    scope="automation:script:update",
                    reason="Administrator saved an Automation Center script draft",
                    idempotency_key=f"automation-script-edit:{script_id}:{token}",
                ),
            ),
        )
    except (DomainError, ValueError) as exc:
        return templates.TemplateResponse(
            "admin/automation/script_builder.html",
            _script_form_context(
                request,
                db,
                kind=detail.kind,
                error=str(exc),
                name=name,
                key=detail.key,
                description=description,
                target_type=detail.target_type,
                event_name=detail.event_name,
                source_code=source_code,
                script_id=detail.script_id,
                source_version=detail.draft_version or detail.active_version,
                return_to=f"/admin/automation/scripts/{script_id}",
            ),
            status_code=400,
        )
    return _automation_redirect(
        path=target,
        notice=f"Script draft v{outcome.version} saved; publish it to activate the change",
    )


@router.post(
    "/scripts/{script_id}/status",
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(automation_scripts.SCRIPT_READ_PERMISSION)),
        Depends(require_permission(automation_scripts.SCRIPT_UPDATE_PERMISSION)),
    ],
)
def update_automation_script_status(
    script_id: UUID,
    request: Request,
    status: str = Form(...),
    command_token: str = Form(...),
    return_to: str = Form(default=""),
    db: Session = Depends(get_db),
    auth: dict = Depends(
        require_permission(automation_scripts.SCRIPT_UPDATE_PERMISSION)
    ),
):
    """Pause, resume, or retire a script without changing its source."""

    try:
        requested_status = automation_scripts.AutomationScriptStatus(status)
        token = UUID(command_token)
        db_session_adapter.release_read_transaction(db)
        outcome = automation_scripts.set_script_status(
            db,
            automation_scripts.SetAutomationScriptStatusCommand(
                tenant_id=OPERATOR_TENANT_ID,
                script_id=script_id,
                status=requested_status,
                permission_keys=frozenset(auth.get("permission_keys") or ()),
                context=CommandContext.system(
                    actor=_actor(request),
                    scope="automation:script:update",
                    reason=f"Administrator changed an Automation Center script to {requested_status.value}",
                    idempotency_key=f"automation-script-status:{script_id}:{requested_status.value}:{token}",
                ),
            ),
        )
    except (DomainError, ValueError) as exc:
        return _automation_redirect(
            path=return_to or f"/admin/automation/scripts/{script_id}", error=str(exc)
        )
    return _automation_redirect(
        path=return_to or f"/admin/automation/scripts/{script_id}",
        notice=f"Script {outcome.script_id} is now {outcome.status.value}",
    )


@router.post(
    "/scripts/{script_id}/publish",
    dependencies=[
        Depends(require_permission("automation:hub:read")),
        Depends(require_permission(automation_scripts.SCRIPT_PUBLISH_PERMISSION)),
    ],
)
def publish_automation_script(
    script_id: UUID,
    request: Request,
    command_token: str = Form(...),
    return_to: str = Form(default="/admin/automation"),
    db: Session = Depends(get_db),
    auth: dict = Depends(
        require_permission(automation_scripts.SCRIPT_PUBLISH_PERMISSION)
    ),
):
    """Publish a script only after the typed owner validates its runtime."""

    try:
        token = UUID(command_token)
        db_session_adapter.release_read_transaction(db)
        outcome = automation_scripts.publish_script(
            db,
            automation_scripts.PublishAutomationScriptCommand(
                tenant_id=web_automation_center.OPERATOR_TENANT_ID,
                script_id=script_id,
                permission_keys=frozenset(auth.get("permission_keys") or ()),
                context=CommandContext.system(
                    actor=_actor(request),
                    scope="automation:script:publish",
                    reason="Administrator activated an Automation Center script",
                    idempotency_key=f"automation-script-publish:{script_id}:{token}",
                ),
            ),
        )
    except (DomainError, ValueError) as exc:
        return _automation_redirect(path=return_to, error=str(exc))
    return _automation_redirect(
        path=return_to,
        notice=f"Script {outcome.script_id} published at version {outcome.version}",
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
    trigger_keys_json: str = Form(default="[]"),
    conditions_json: str = Form(default="[]"),
    actions_json: str = Form(default="[]"),
    schedule_json: str = Form(default="{}"),
    customer_scope: str = Form(default="company"),
    customer_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(RULE_CREATE_PERMISSION)),
):
    """Save an app-approved combination of conditions and actions as a draft."""

    selected_trigger_keys = _form_trigger_keys(trigger_key, trigger_keys_json)
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
                trigger_key=trigger_key,
                trigger_keys=selected_trigger_keys,
                customer_scope=customer_scope,
                customer_ids=_preserved_customer_ids(customer_ids),
                permission_keys=frozenset(auth.get("permission_keys") or ()),
            ),
            status_code=400,
        )

    try:
        conditions, actions = _form_definition(
            trigger_key=trigger_key,
            trigger_keys=selected_trigger_keys,
            conditions_json=conditions_json,
            actions_json=actions_json,
            customer_ids=selected_customer_ids,
        )
        schedule = _form_schedule(schedule_json)
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
                trigger_keys=selected_trigger_keys,
                conditions=conditions,
                actions=actions,
                schedule=schedule,
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
                trigger_keys=selected_trigger_keys,
                customer_scope=customer_scope,
                customer_ids=selected_customer_ids,
                conditions=_safe_json_conditions(conditions_json),
                actions=_safe_json_list(actions_json),
                schedule=_safe_json_schedule(schedule_json),
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
        automation_capabilities.trigger_capability(trigger_key).author_permission
        for trigger_key in state.trigger_keys
    }
    required_permissions.update(
        automation_capabilities.action_capability(item.action_key).author_permission
        for item in state.actions
    )
    if "*" not in permission_keys and not required_permissions.issubset(
        permission_keys
    ):
        return _automation_redirect(
            error="Your account is missing a permission required to edit this rule."
        )
    conditions = _editor_condition_value(state.conditions) or ()
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
            trigger_keys=state.trigger_keys,
            customer_scope="selected" if state.customer_ids else "company",
            customer_ids=state.customer_ids,
            conditions=conditions,
            actions=actions,
            schedule=state.schedule,
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
    trigger_keys_json: str = Form(default="[]"),
    conditions_json: str = Form(default="[]"),
    actions_json: str = Form(default="[]"),
    schedule_json: str = Form(default="{}"),
    customer_scope: str = Form(default="company"),
    customer_ids: list[str] = Form(default=[]),
    command_token: str = Form(...),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission(RULE_UPDATE_PERMISSION)),
):
    selected_trigger_keys = _form_trigger_keys(trigger_key, trigger_keys_json)
    try:
        selected_customer_ids = _customer_selection(
            db, customer_scope=customer_scope, customer_ids=customer_ids
        )
        conditions, actions = _form_definition(
            trigger_key=trigger_key,
            trigger_keys=selected_trigger_keys,
            conditions_json=conditions_json,
            actions_json=actions_json,
            customer_ids=selected_customer_ids,
        )
        schedule = _form_schedule(schedule_json)
        token = UUID(command_token)
    except (ValueError, TypeError) as exc:
        return templates.TemplateResponse(
            "admin/automation/rule_builder.html",
            _generic_form_context(
                request,
                db,
                error=str(exc) or "The form is no longer valid. Reload and try again.",
                trigger_key=trigger_key,
                trigger_keys=selected_trigger_keys,
                customer_scope=customer_scope,
                customer_ids=_preserved_customer_ids(customer_ids),
                schedule=_safe_json_schedule(schedule_json),
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
                trigger_keys=selected_trigger_keys,
                conditions=conditions,
                actions=actions,
                schedule=schedule,
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
                trigger_keys=selected_trigger_keys,
                customer_scope=customer_scope,
                customer_ids=selected_customer_ids,
                conditions=_safe_json_conditions(conditions_json),
                actions=_safe_json_list(actions_json),
                schedule=_safe_json_schedule(schedule_json),
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
    return_to: str = Form(default="/admin/automation"),
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
        return _automation_redirect(path=return_to, error=str(exc))
    return _automation_redirect(path=return_to)


def _change_rule_status(
    *,
    rule_id: UUID,
    request: Request,
    db: Session,
    auth: dict,
    operation: automation_rules.AutomationRuleOperation,
    command_token: str,
    return_to: str,
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
        return _automation_redirect(path=return_to, error=str(exc))
    return _automation_redirect(path=return_to)


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
    return_to: str = Form(default="/admin/automation"),
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
        return_to=return_to,
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
    return_to: str = Form(default="/admin/automation"),
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
        return_to=return_to,
    )


__all__ = ["router"]
