"""Whitelisted dynamic-filter specs for projects and project tasks.

Backs the `filters` JSON param on GET /projects and GET /project-tasks
(Phase 3 §2.1 — the CRM API exposed the same param via its filter engine).
Reuses the shared `dynamic_filters` engine, mirroring
`support_ticket_filters.py`: AND rows plus OR groups, strict field/operator
whitelisting — never raw column injection.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import String, cast, exists, or_, select
from sqlalchemy.sql.elements import ClauseElement, ColumnElement

from app.models.project import Project, ProjectTask, ProjectTaskAssignee
from app.services.dynamic_filters import (
    NULL_TOKENS,
    FilterFieldSpec,
    FilterValidationError,
    _coerce_list,
    _coerce_scalar,
    build_filter_expression,
    parse_filter_payload,
)

PROJECT_DOCTYPE = "Project"
PROJECT_TASK_DOCTYPE = "Project Task"


def _task_assigned_to_expression(operator: str, value: object) -> ClauseElement:
    """Match the legacy single-assignee column or the assignees table."""

    def _match(person_id: object) -> ColumnElement[bool]:
        coerced = _coerce_scalar(person_id, "uuid")
        return or_(
            ProjectTask.assigned_to_person_id == coerced,
            exists(
                select(ProjectTaskAssignee.task_id)
                .where(ProjectTaskAssignee.task_id == ProjectTask.id)
                .where(ProjectTaskAssignee.person_id == coerced)
            ),
        )

    if operator in {"is", "is not"}:
        token = str(value).strip().lower() if value is not None else None
        if token not in NULL_TOKENS:
            raise FilterValidationError(
                "assigned_to_person_id supports only NULL checks for 'is'/'is not'"
            )
        unassigned = ProjectTask.assigned_to_person_id.is_(None) & ~exists(
            select(ProjectTaskAssignee.task_id).where(
                ProjectTaskAssignee.task_id == ProjectTask.id
            )
        )
        return unassigned if operator == "is" else ~unassigned

    if operator in {"in", "not in"}:
        matches = or_(*[_match(item) for item in _coerce_list(value, "uuid")])
        return matches if operator == "in" else ~matches

    if operator == "=":
        return _match(value)
    if operator == "!=":
        return ~_match(value)
    raise FilterValidationError(
        f"Operator '{operator}' is not allowed for assigned_to_person_id"
    )


def _tags_expression_for(column) -> Callable[[str, object], ClauseElement]:
    """Tag matching against the JSON tags array via its text serialization."""

    def _expression(operator: str, value: object) -> ClauseElement:
        tags_text = cast(column, String)
        if operator in {"=", "!="}:
            token = str(_coerce_scalar(value, "text") or "").strip()
            if not token:
                raise FilterValidationError("Tag value cannot be empty")
            pattern = f'%"{token}"%'
            matched = tags_text.ilike(pattern)
            return matched if operator == "=" else ~matched
        if operator in {"like", "not like"}:
            token = str(_coerce_scalar(value, "text") or "").strip()
            if not token:
                raise FilterValidationError("Tag value cannot be empty")
            pattern = f"%{token}%"
            matched = tags_text.ilike(pattern)
            return matched if operator == "like" else ~matched
        raise FilterValidationError(f"Operator '{operator}' is not allowed for tags")

    return _expression


PROJECT_FILTER_SPECS: dict[str, FilterFieldSpec] = {
    "name": FilterFieldSpec(field="name", expression=Project.name, field_type="text"),
    "code": FilterFieldSpec(field="code", expression=Project.code, field_type="text"),
    "number": FilterFieldSpec(
        field="number", expression=Project.number, field_type="text"
    ),
    "status": FilterFieldSpec(
        field="status", expression=Project.status, field_type="select"
    ),
    "priority": FilterFieldSpec(
        field="priority", expression=Project.priority, field_type="select"
    ),
    "project_type": FilterFieldSpec(
        field="project_type", expression=Project.project_type, field_type="select"
    ),
    "region": FilterFieldSpec(
        field="region", expression=Project.region, field_type="text"
    ),
    "customer_address": FilterFieldSpec(
        field="customer_address",
        expression=Project.customer_address,
        field_type="text",
    ),
    "subscriber_id": FilterFieldSpec(
        field="subscriber_id", expression=Project.subscriber_id, field_type="uuid"
    ),
    "lead_id": FilterFieldSpec(
        field="lead_id", expression=Project.lead_id, field_type="uuid"
    ),
    "service_team_id": FilterFieldSpec(
        field="service_team_id",
        expression=Project.service_team_id,
        field_type="uuid",
    ),
    "project_template_id": FilterFieldSpec(
        field="project_template_id",
        expression=Project.project_template_id,
        field_type="uuid",
    ),
    "created_by_person_id": FilterFieldSpec(
        field="created_by_person_id",
        expression=Project.created_by_person_id,
        field_type="uuid",
    ),
    "owner_person_id": FilterFieldSpec(
        field="owner_person_id",
        expression=Project.owner_person_id,
        field_type="uuid",
    ),
    "manager_person_id": FilterFieldSpec(
        field="manager_person_id",
        expression=Project.manager_person_id,
        field_type="uuid",
    ),
    "project_manager_person_id": FilterFieldSpec(
        field="project_manager_person_id",
        expression=Project.project_manager_person_id,
        field_type="uuid",
    ),
    "assistant_manager_person_id": FilterFieldSpec(
        field="assistant_manager_person_id",
        expression=Project.assistant_manager_person_id,
        field_type="uuid",
    ),
    "tags": FilterFieldSpec(
        field="tags",
        field_type="text",
        operators={"=", "!=", "like", "not like"},
        builder=_tags_expression_for(Project.tags),
    ),
    "start_at": FilterFieldSpec(
        field="start_at", expression=Project.start_at, field_type="datetime"
    ),
    "due_at": FilterFieldSpec(
        field="due_at", expression=Project.due_at, field_type="datetime"
    ),
    "completed_at": FilterFieldSpec(
        field="completed_at", expression=Project.completed_at, field_type="datetime"
    ),
    "created_at": FilterFieldSpec(
        field="created_at", expression=Project.created_at, field_type="datetime"
    ),
    "updated_at": FilterFieldSpec(
        field="updated_at", expression=Project.updated_at, field_type="datetime"
    ),
    "is_active": FilterFieldSpec(
        field="is_active", expression=Project.is_active, field_type="boolean"
    ),
}


PROJECT_TASK_FILTER_SPECS: dict[str, FilterFieldSpec] = {
    "title": FilterFieldSpec(
        field="title", expression=ProjectTask.title, field_type="text"
    ),
    "number": FilterFieldSpec(
        field="number", expression=ProjectTask.number, field_type="text"
    ),
    "status": FilterFieldSpec(
        field="status", expression=ProjectTask.status, field_type="select"
    ),
    "priority": FilterFieldSpec(
        field="priority", expression=ProjectTask.priority, field_type="select"
    ),
    "project_id": FilterFieldSpec(
        field="project_id", expression=ProjectTask.project_id, field_type="uuid"
    ),
    "parent_task_id": FilterFieldSpec(
        field="parent_task_id",
        expression=ProjectTask.parent_task_id,
        field_type="uuid",
    ),
    "template_task_id": FilterFieldSpec(
        field="template_task_id",
        expression=ProjectTask.template_task_id,
        field_type="uuid",
    ),
    "assigned_to_person_id": FilterFieldSpec(
        field="assigned_to_person_id",
        field_type="uuid",
        operators={"=", "!=", "in", "not in", "is", "is not"},
        builder=_task_assigned_to_expression,
    ),
    "created_by_person_id": FilterFieldSpec(
        field="created_by_person_id",
        expression=ProjectTask.created_by_person_id,
        field_type="uuid",
    ),
    "ticket_id": FilterFieldSpec(
        field="ticket_id", expression=ProjectTask.ticket_id, field_type="uuid"
    ),
    "work_order_id": FilterFieldSpec(
        field="work_order_id",
        expression=ProjectTask.work_order_id,
        field_type="uuid",
    ),
    "effort_hours": FilterFieldSpec(
        field="effort_hours",
        expression=ProjectTask.effort_hours,
        field_type="number",
    ),
    "tags": FilterFieldSpec(
        field="tags",
        field_type="text",
        operators={"=", "!=", "like", "not like"},
        builder=_tags_expression_for(ProjectTask.tags),
    ),
    "start_at": FilterFieldSpec(
        field="start_at", expression=ProjectTask.start_at, field_type="datetime"
    ),
    "due_at": FilterFieldSpec(
        field="due_at", expression=ProjectTask.due_at, field_type="datetime"
    ),
    "completed_at": FilterFieldSpec(
        field="completed_at",
        expression=ProjectTask.completed_at,
        field_type="datetime",
    ),
    "created_at": FilterFieldSpec(
        field="created_at", expression=ProjectTask.created_at, field_type="datetime"
    ),
    "updated_at": FilterFieldSpec(
        field="updated_at", expression=ProjectTask.updated_at, field_type="datetime"
    ),
    "is_active": FilterFieldSpec(
        field="is_active", expression=ProjectTask.is_active, field_type="boolean"
    ),
}


def build_project_filter_clause(
    filters: str | list | dict | None,
) -> ColumnElement[bool] | None:
    """Parse a `filters` payload and build the whitelisted WHERE clause.

    Raises FilterValidationError on any invalid payload, field, operator,
    or value — callers translate this into an HTTP 400.
    """
    filter_query = parse_filter_payload(filters, default_doctype=PROJECT_DOCTYPE)
    return build_filter_expression(
        filter_query,
        doctype=PROJECT_DOCTYPE,
        field_specs=PROJECT_FILTER_SPECS,
    )


def build_project_task_filter_clause(
    filters: str | list | dict | None,
) -> ColumnElement[bool] | None:
    filter_query = parse_filter_payload(filters, default_doctype=PROJECT_TASK_DOCTYPE)
    return build_filter_expression(
        filter_query,
        doctype=PROJECT_TASK_DOCTYPE,
        field_specs=PROJECT_TASK_FILTER_SPECS,
    )
