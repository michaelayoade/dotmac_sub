from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import and_, or_
from sqlalchemy.sql.elements import BinaryExpression, ClauseElement, ColumnElement


FilterExpressionBuilder = Callable[[str, Any], ClauseElement]


class FilterValidationError(ValueError):
    """Raised when filter payload or values are invalid."""


@dataclass(frozen=True)
class FilterFieldSpec:
    """Whitelist definition for a filterable field."""

    field: str
    expression: ColumnElement | None = None
    field_type: str = "text"
    operators: set[str] | None = None
    options: set[str] | None = None
    builder: FilterExpressionBuilder | None = None


@dataclass(frozen=True)
class FilterCondition:
    """Normalized ERPNext-style filter row."""

    doctype: str
    field: str
    operator: str
    value: Any


@dataclass(frozen=True)
class FilterQuery:
    """Normalized filter payload with default AND and optional OR group."""

    and_filters: list[FilterCondition] = field(default_factory=list)
    or_filters: list[FilterCondition] = field(default_factory=list)


OPERATOR_LABELS: dict[str, str] = {
    "=": "Equals",
    "!=": "Not Equals",
    "like": "Like",
    "not like": "Not Like",
    "in": "In",
    "not in": "Not In",
    ">": "Greater Than",
    "<": "Less Than",
    ">=": "Greater Than or Equal",
    "<=": "Less Than or Equal",
    "is": "Is",
    "is not": "Is Not",
    "between": "Between",
}


DEFAULT_OPERATORS_BY_TYPE: dict[str, set[str]] = {
    "text": {"=", "!=", "like", "not like", "in", "not in", "is", "is not"},
    "select": {"=", "!=", "in", "not in", "is", "is not"},
    "uuid": {"=", "!=", "in", "not in", "is", "is not"},
    "number": {"=", "!=", ">", "<", ">=", "<=", "in", "not in", "is", "is not"},
    "boolean": {"=", "!=", "is", "is not"},
    "date": {"=", "!=", ">", "<", ">=", "<=", "between", "is", "is not"},
    "datetime": {"=", "!=", ">", "<", ">=", "<=", "between", "is", "is not"},
}


NULL_TOKENS = {None, "", "null", "none", "nil"}
TRUE_TOKENS = {True, "true", "1", 1, "yes", "on"}
FALSE_TOKENS = {False, "false", "0", 0, "no", "off"}


def _parse_bool(value: Any) -> bool:
    normalized = str(value).strip().lower() if value is not None else value
    if normalized in TRUE_TOKENS:
        return True
    if normalized in FALSE_TOKENS:
        return False
    raise FilterValidationError("Expected a boolean value")


def _coerce_scalar(value: Any, field_type: str) -> Any:
    if field_type in {"text", "select"}:
        if value is None:
            return None
        return str(value)

    if field_type == "uuid":
        if isinstance(value, UUID):
            return value
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise FilterValidationError("Expected a valid UUID value") from exc

    if field_type == "number":
        if isinstance(value, (int, float)):
            return value
        try:
            text = str(value).strip()
            return float(text) if "." in text else int(text)
        except (TypeError, ValueError) as exc:
            raise FilterValidationError("Expected a numeric value") from exc

    if field_type == "boolean":
        return _parse_bool(value)

    if field_type == "date":
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        try:
            return date.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise FilterValidationError("Expected an ISO date value (YYYY-MM-DD)") from exc

    if field_type == "datetime":
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise FilterValidationError("Expected an ISO datetime value") from exc

    raise FilterValidationError(f"Unsupported field type: {field_type}")


def _coerce_list(value: Any, field_type: str) -> list[Any]:
    if isinstance(value, list):
        raw_values = value
    elif isinstance(value, tuple):
        raw_values = list(value)
    elif isinstance(value, str):
        raw_values = [item.strip() for item in value.split(",") if item.strip()]
    else:
        raise FilterValidationError("Expected an array value")

    if not raw_values:
        raise FilterValidationError("Array filter value cannot be empty")

    return [_coerce_scalar(item, field_type) for item in raw_values]


def _normalized_operator(operator: Any) -> str:
    if operator is None:
        raise FilterValidationError("Filter operator is required")
    op = str(operator).strip().lower()
    if op not in OPERATOR_LABELS:
        raise FilterValidationError(f"Unsupported operator: {operator}")
    return op


def parse_filter_payload(payload: str | list | dict | None, *, default_doctype: str) -> FilterQuery:
    """Parse ERPNext-style filters with optional OR grouping.

    Accepted payloads:
    - list of rows: [[doctype, field, op, value], ...]
    - dict with optional keys `and` (list of rows) and `or` (list of rows)
    """
    if payload is None or payload == "":
        return FilterQuery()

    parsed: Any
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise FilterValidationError("Invalid JSON in filters payload") from exc
    else:
        parsed = payload

    if isinstance(parsed, list):
        and_rows = parsed
        or_rows: list[Any] = []
    elif isinstance(parsed, dict):
        and_rows = parsed.get("and", [])
        or_rows = parsed.get("or", [])
        if not isinstance(and_rows, list) or not isinstance(or_rows, list):
            raise FilterValidationError("Filter groups 'and'/'or' must be arrays")
    else:
        raise FilterValidationError("Filters payload must be a list or object")

    def _parse_rows(rows: list[Any]) -> list[FilterCondition]:
        conditions: list[FilterCondition] = []
        for row in rows:
            if not isinstance(row, list) or len(row) != 4:
                raise FilterValidationError(
                    "Each filter must be [doctype, field, operator, value]"
                )
            doctype, field_name, operator, value = row
            doc_name = str(doctype).strip() if doctype else default_doctype
            field_key = str(field_name).strip()
            if not field_key:
                raise FilterValidationError("Filter field is required")
            conditions.append(
                FilterCondition(
                    doctype=doc_name,
                    field=field_key,
                    operator=_normalized_operator(operator),
                    value=value,
                )
            )
        return conditions

    return FilterQuery(and_filters=_parse_rows(and_rows), or_filters=_parse_rows(or_rows))


def _validate_field_doctype(condition: FilterCondition, expected_doctype: str) -> None:
    if condition.doctype.lower() != expected_doctype.lower():
        raise FilterValidationError(
            f"Filter doctype '{condition.doctype}' is not allowed for this endpoint"
        )


def _allowed_ops(spec: FilterFieldSpec) -> set[str]:
    if spec.operators is not None:
        return spec.operators
    return DEFAULT_OPERATORS_BY_TYPE.get(spec.field_type, DEFAULT_OPERATORS_BY_TYPE["text"])


def _validate_select_option(spec: FilterFieldSpec, value: Any) -> None:
    if spec.options is None:
        return

    options = {str(item) for item in spec.options}
    if isinstance(value, list):
        invalid = [item for item in value if str(item) not in options]
        if invalid:
            raise FilterValidationError(
                f"Invalid option(s) for '{spec.field}': {', '.join(str(v) for v in invalid)}"
            )
        return

    if value is not None and str(value) not in options:
        raise FilterValidationError(f"Invalid option for '{spec.field}': {value}")


def _build_default_expression(
    spec: FilterFieldSpec,
    *,
    operator: str,
    value: Any,
) -> ClauseElement:
    if spec.expression is None:
        raise FilterValidationError(f"Field '{spec.field}' is missing an expression")

    col = spec.expression
    field_type = spec.field_type

    if operator in {"like", "not like"}:
        if field_type not in {"text", "select", "uuid"}:
            raise FilterValidationError(f"Operator '{operator}' is not allowed for {field_type} fields")
        pattern = f"%{_coerce_scalar(value, 'text')}%"
        return col.ilike(pattern) if operator == "like" else ~col.ilike(pattern)

    if operator in {"in", "not in"}:
        coerced = _coerce_list(value, field_type)
        _validate_select_option(spec, coerced)
        return col.in_(coerced) if operator == "in" else ~col.in_(coerced)

    if operator == "between":
        if field_type not in {"date", "datetime"}:
            raise FilterValidationError("Between is only supported for date/datetime fields")
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise FilterValidationError("Between expects [from, to]")
        start = _coerce_scalar(value[0], field_type)
        end = _coerce_scalar(value[1], field_type)
        return col.between(start, end)

    if operator in {"is", "is not"}:
        token = str(value).strip().lower() if value is not None else None
        if token in NULL_TOKENS:
            return col.is_(None) if operator == "is" else col.is_not(None)
        if field_type == "boolean":
            parsed_bool = _parse_bool(value)
            return col.is_(parsed_bool) if operator == "is" else col.is_not(parsed_bool)
        raise FilterValidationError(
            f"Operator '{operator}' only supports NULL checks and boolean fields"
        )

    scalar = _coerce_scalar(value, field_type)
    _validate_select_option(spec, scalar)

    if operator == "=":
        return col == scalar
    if operator == "!=":
        return col != scalar
    if operator == ">":
        return col > scalar
    if operator == "<":
        return col < scalar
    if operator == ">=":
        return col >= scalar
    if operator == "<=":
        return col <= scalar

    raise FilterValidationError(f"Unsupported operator: {operator}")


def build_filter_expression(
    filter_query: FilterQuery,
    *,
    doctype: str,
    field_specs: dict[str, FilterFieldSpec],
) -> BinaryExpression | None:
    """Build a SQLAlchemy filter expression from parsed dynamic filters."""
    and_conditions: list[ClauseElement] = []
    or_conditions: list[ClauseElement] = []

    def _build_condition(condition: FilterCondition) -> ClauseElement:
        _validate_field_doctype(condition, doctype)

        spec = field_specs.get(condition.field)
        if spec is None:
            raise FilterValidationError(f"Field '{condition.field}' is not filterable")

        allowed_ops = _allowed_ops(spec)
        if condition.operator not in allowed_ops:
            raise FilterValidationError(
                f"Operator '{condition.operator}' is not allowed for field '{condition.field}'"
            )

        if spec.builder is not None:
            return spec.builder(condition.operator, condition.value)

        return _build_default_expression(
            spec,
            operator=condition.operator,
            value=condition.value,
        )

    and_conditions.extend(_build_condition(item) for item in filter_query.and_filters)
    or_conditions.extend(_build_condition(item) for item in filter_query.or_filters)

    final_conditions: list[ClauseElement] = []
    if and_conditions:
        final_conditions.append(and_(*and_conditions))
    if or_conditions:
        final_conditions.append(or_(*or_conditions))

    if not final_conditions:
        return None

    if len(final_conditions) == 1:
        return final_conditions[0]

    return and_(*final_conditions)


def build_sort_clause(
    *,
    order_by: str | None,
    order_dir: str | None,
    allowed_sort_fields: dict[str, ColumnElement],
    default_field: str,
    default_dir: str = "asc",
) -> ClauseElement:
    """Build a validated ORDER BY clause with strict field allow-list."""
    sort_field = (order_by or default_field).strip()
    direction = (order_dir or default_dir).strip().lower()

    if sort_field not in allowed_sort_fields:
        raise FilterValidationError(f"Sort field '{sort_field}' is not allowed")
    if direction not in {"asc", "desc"}:
        raise FilterValidationError("Sort direction must be 'asc' or 'desc'")

    col = allowed_sort_fields[sort_field]
    return col.asc() if direction == "asc" else col.desc()
