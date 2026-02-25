from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import case, func, literal, or_
from sqlalchemy.orm import Query, Session

from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.table_column_config import TableColumnConfig
from app.models.table_column_default_config import TableColumnDefaultConfig
from app.schemas.table_config import (
    TableColumnAvailable,
    TableColumnPreference,
    TableColumnResolved,
)

ReservedParamKeys = {
    "limit",
    "offset",
    "sort_by",
    "sort_dir",
    "table_key",
    "q",
    "_ts",
}

ExpressionResolver = Callable[[type], Any]
FilterResolver = Callable[[Query, type, Any], Query]


@dataclass(frozen=True)
class TableFieldDefinition:
    key: str
    label: str
    sortable: bool = False
    filterable: bool = False
    hidden_by_default: bool = False
    expression_resolver: ExpressionResolver | None = None
    filter_resolver: FilterResolver | None = None
    is_computed: bool = False

    def expression(self, model: type) -> Any:
        if self.expression_resolver is not None:
            return self.expression_resolver(model)
        if hasattr(model, self.key):
            return getattr(model, self.key)
        raise HTTPException(status_code=400, detail=f"Field has no expression mapping: {self.key}")


@dataclass(frozen=True)
class TableDefinition:
    table_key: str
    model: type
    fields: tuple[TableFieldDefinition, ...]
    row_meta_fields: tuple[str, ...] = ()


class TableRegistry:
    _tables: dict[str, TableDefinition] = {}

    @classmethod
    def register(
        cls,
        *,
        table_key: str,
        model: type,
        allowed_fields: list[str],
        computed_fields: list[TableFieldDefinition] | None = None,
        field_overrides: dict[str, dict[str, Any]] | None = None,
        row_meta_fields: list[str] | None = None,
    ) -> None:
        if not table_key:
            raise ValueError("table_key is required")

        overrides = field_overrides or {}
        computed = computed_fields or []
        seen_keys: set[str] = set()
        fields: list[TableFieldDefinition] = []

        for field_key in allowed_fields:
            if field_key in seen_keys:
                raise ValueError(f"Duplicate field in registry: {field_key}")
            if not hasattr(model, field_key):
                raise ValueError(f"Field {field_key} is not present on model {model.__name__}")

            meta = overrides.get(field_key, {})
            fields.append(
                TableFieldDefinition(
                    key=field_key,
                    label=meta.get("label", field_key.replace("_", " ").title()),
                    sortable=bool(meta.get("sortable", True)),
                    filterable=bool(meta.get("filterable", True)),
                    hidden_by_default=bool(meta.get("hidden_by_default", False)),
                )
            )
            seen_keys.add(field_key)

        for computed_field in computed:
            if computed_field.key in seen_keys:
                raise ValueError(f"Duplicate computed field key: {computed_field.key}")
            fields.append(computed_field)
            seen_keys.add(computed_field.key)

        cls._tables[table_key] = TableDefinition(
            table_key=table_key,
            model=model,
            fields=tuple(fields),
            row_meta_fields=tuple(row_meta_fields or []),
        )

    @classmethod
    def get(cls, table_key: str) -> TableDefinition:
        definition = cls._tables.get(table_key)
        if not definition:
            raise HTTPException(status_code=404, detail="Unregistered tableKey")
        return definition

    @classmethod
    def exists(cls, table_key: str) -> bool:
        return table_key in cls._tables


class TableConfigurationService:
    @staticmethod
    def _field_map(definition: TableDefinition) -> dict[str, TableFieldDefinition]:
        return {field.key: field for field in definition.fields}

    @staticmethod
    def _default_state(definition: TableDefinition) -> dict[str, dict[str, Any]]:
        state: dict[str, dict[str, Any]] = {}
        for index, field in enumerate(definition.fields):
            state[field.key] = {
                "display_order": index,
                "is_visible": not field.hidden_by_default,
            }
        return state

    @staticmethod
    def _system_default_configs(
        db: Session, table_key: str
    ) -> list[TableColumnDefaultConfig]:
        return (
            db.query(TableColumnDefaultConfig)
            .filter(TableColumnDefaultConfig.table_key == table_key)
            .all()
        )

    @staticmethod
    def get_columns(db: Session, user_id: UUID, table_key: str) -> list[TableColumnResolved]:
        definition = TableRegistry.get(table_key)
        state = TableConfigurationService._default_state(definition)

        default_configs = TableConfigurationService._system_default_configs(db, table_key)
        for config in default_configs:
            if config.column_key in state:
                state[config.column_key] = {
                    "display_order": config.display_order,
                    "is_visible": config.is_visible,
                }

        user_configs = (
            db.query(TableColumnConfig)
            .filter(TableColumnConfig.user_id == user_id)
            .filter(TableColumnConfig.table_key == table_key)
            .all()
        )

        for config in user_configs:
            if config.column_key in state:
                state[config.column_key] = {
                    "display_order": config.display_order,
                    "is_visible": config.is_visible,
                }

        if not default_configs and not user_configs:
            if table_key == "customers" and "customer_name" in state:
                preferred_order = [
                    "customer_name",
                    "id",
                    "status",
                    "customer_type",
                ]
                base_order = len(preferred_order)
                for key, config in state.items():
                    if key in preferred_order:
                        config["display_order"] = preferred_order.index(key)
                        config["is_visible"] = True
                    else:
                        config["display_order"] = base_order + config["display_order"]
                        config["is_visible"] = False
            if table_key == "subscribers" and "subscriber_name" in state:
                preferred_order = [
                    "subscriber_number",
                    "subscriber_name",
                    "status",
                    "reseller_id",
                ]
                base_order = len(preferred_order)
                for key, config in state.items():
                    if key in preferred_order:
                        config["display_order"] = preferred_order.index(key)
                        config["is_visible"] = True
                    else:
                        config["display_order"] = base_order + config["display_order"]
                        config["is_visible"] = False

        registry_order = {field.key: i for i, field in enumerate(definition.fields)}
        resolved = [
            TableColumnResolved(
                column_key=field.key,
                label=field.label,
                sortable=field.sortable,
                display_order=state[field.key]["display_order"],
                is_visible=state[field.key]["is_visible"],
            )
            for field in definition.fields
        ]
        resolved.sort(
            key=lambda column: (
                column.display_order,
                registry_order[column.column_key],
            )
        )
        return resolved

    @staticmethod
    def save_system_default_columns(
        db: Session,
        table_key: str,
        payload: list[TableColumnPreference],
    ) -> list[TableColumnResolved]:
        definition = TableRegistry.get(table_key)
        field_map = TableConfigurationService._field_map(definition)
        state = TableConfigurationService._default_state(definition)

        seen: set[str] = set()
        for item in payload:
            if item.column_key not in field_map:
                raise HTTPException(status_code=400, detail=f"Invalid column_key: {item.column_key}")
            if item.column_key in seen:
                raise HTTPException(status_code=400, detail=f"Duplicate column_key: {item.column_key}")
            seen.add(item.column_key)
            state[item.column_key] = {
                "display_order": item.display_order,
                "is_visible": item.is_visible,
            }

        specified_keys = {item.column_key for item in payload}
        max_specified_order = max((item.display_order for item in payload), default=-1)
        if specified_keys:
            for key, config in state.items():
                if key not in specified_keys:
                    config["display_order"] = max_specified_order + 1 + config["display_order"]

        if not any(item["is_visible"] for item in state.values()):
            raise HTTPException(status_code=400, detail="At least one column must be visible")

        normalized = sorted(state.items(), key=lambda kv: kv[1]["display_order"])

        (
            db.query(TableColumnDefaultConfig)
            .filter(TableColumnDefaultConfig.table_key == table_key)
            .delete(synchronize_session=False)
        )

        for index, (column_key, config) in enumerate(normalized):
            db.add(
                TableColumnDefaultConfig(
                    table_key=table_key,
                    column_key=column_key,
                    display_order=index,
                    is_visible=config["is_visible"],
                )
            )

        db.commit()
        # Return defaults as visible to a pseudo-user via resolved state.
        # Actual callers should use get_columns(user_id=...) for full hierarchy.
        return [
            TableColumnResolved(
                column_key=field.key,
                label=field.label,
                sortable=field.sortable,
                display_order=state[field.key]["display_order"],
                is_visible=state[field.key]["is_visible"],
            )
            for field in definition.fields
        ]

    @staticmethod
    def get_available_columns(table_key: str) -> list[TableColumnAvailable]:
        definition = TableRegistry.get(table_key)
        return [
            TableColumnAvailable(
                key=field.key,
                label=field.label,
                sortable=field.sortable,
                hidden_by_default=field.hidden_by_default,
            )
            for field in definition.fields
        ]

    @staticmethod
    def save_columns(
        db: Session,
        user_id: UUID,
        table_key: str,
        payload: list[TableColumnPreference],
    ) -> list[TableColumnResolved]:
        definition = TableRegistry.get(table_key)
        field_map = TableConfigurationService._field_map(definition)

        # True reset path: remove user overrides so hierarchy falls back
        # to system default, then registry default.
        if not payload:
            (
                db.query(TableColumnConfig)
                .filter(TableColumnConfig.user_id == user_id)
                .filter(TableColumnConfig.table_key == table_key)
                .delete(synchronize_session=False)
            )
            db.commit()
            return TableConfigurationService.get_columns(db, user_id, table_key)

        seen: set[str] = set()
        for item in payload:
            if item.column_key not in field_map:
                raise HTTPException(status_code=400, detail=f"Invalid column_key: {item.column_key}")
            if item.column_key in seen:
                raise HTTPException(status_code=400, detail=f"Duplicate column_key: {item.column_key}")
            seen.add(item.column_key)

        state = TableConfigurationService._default_state(definition)
        for item in payload:
            state[item.column_key] = {
                "display_order": item.display_order,
                "is_visible": item.is_visible,
            }

        specified_keys = {item.column_key for item in payload}
        max_specified_order = max((item.display_order for item in payload), default=-1)
        if specified_keys:
            for key, config in state.items():
                if key not in specified_keys:
                    config["display_order"] = max_specified_order + 1 + config["display_order"]

        if not any(item["is_visible"] for item in state.values()):
            raise HTTPException(status_code=400, detail="At least one column must be visible")

        normalized = sorted(state.items(), key=lambda kv: kv[1]["display_order"])

        (
            db.query(TableColumnConfig)
            .filter(TableColumnConfig.user_id == user_id)
            .filter(TableColumnConfig.table_key == table_key)
            .delete(synchronize_session=False)
        )

        for index, (column_key, config) in enumerate(normalized):
            db.add(
                TableColumnConfig(
                    user_id=user_id,
                    table_key=table_key,
                    column_key=column_key,
                    display_order=index,
                    is_visible=config["is_visible"],
                )
            )

        db.commit()
        return TableConfigurationService.get_columns(db, user_id, table_key)

    @staticmethod
    def _convert_value(value: Any) -> Any:
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, UUID):
            return str(value)
        if hasattr(value, "value") and not isinstance(value, (str, bytes)):
            return value.value
        return value

    @staticmethod
    def _apply_scalar_filters(
        query: Query,
        model: type,
        field_map: dict[str, TableFieldDefinition],
        request_params: dict[str, Any],
    ) -> Query:
        for key, value in request_params.items():
            if key in ReservedParamKeys or value is None or value == "":
                continue
            field = field_map.get(key)
            if not field or not field.filterable:
                raise HTTPException(status_code=400, detail=f"Invalid filter field: {key}")

            if field.filter_resolver is not None:
                query = field.filter_resolver(query, model, value)
                continue

            expression = field.expression(model)
            if field.key == "status":
                try:
                    value = SubscriberStatus(str(value))
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail="Invalid status filter") from exc
            if isinstance(value, str) and "*" in value:
                query = query.filter(expression.ilike(value.replace("*", "%")))
            else:
                query = query.filter(expression == value)

        return query

    @staticmethod
    def apply_query_config(
        db: Session,
        user_id: UUID,
        table_key: str,
        request_params: dict[str, Any],
    ) -> tuple[list[TableColumnResolved], list[dict[str, Any]], int]:
        definition = TableRegistry.get(table_key)
        field_map = TableConfigurationService._field_map(definition)

        columns = TableConfigurationService.get_columns(db, user_id, table_key)
        visible_columns = [column for column in columns if column.is_visible]
        selected_keys = [column.column_key for column in visible_columns]
        meta_keys = [key for key in definition.row_meta_fields if key in field_map]

        limit = int(request_params.get("limit", 50) or 50)
        offset = int(request_params.get("offset", 0) or 0)
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
        if offset < 0:
            raise HTTPException(status_code=400, detail="offset must be >= 0")

        query = db.query(definition.model)

        q = request_params.get("q")
        if isinstance(q, str) and q.strip():
            like_term = f"%{q.strip()}%"
            search_columns = []
            for field_name in (
                "first_name",
                "last_name",
                "email",
                "subscriber_number",
                "name",
                "code",
            ):
                if hasattr(definition.model, field_name):
                    search_columns.append(getattr(definition.model, field_name).ilike(like_term))
            if search_columns:
                query = query.filter(or_(*search_columns))

        query = TableConfigurationService._apply_scalar_filters(
            query,
            definition.model,
            field_map,
            request_params,
        )

        total = query.count()

        sort_by = str(request_params.get("sort_by") or "created_at")
        sort_dir = str(request_params.get("sort_dir") or "desc").lower()
        if sort_dir not in {"asc", "desc"}:
            raise HTTPException(status_code=400, detail="sort_dir must be asc or desc")

        sort_field = field_map.get(sort_by)
        if not sort_field or not sort_field.sortable:
            raise HTTPException(status_code=400, detail="Invalid sort field")

        sort_expression = sort_field.expression(definition.model)
        query = query.order_by(sort_expression.desc() if sort_dir == "desc" else sort_expression.asc())

        query = query.offset(offset).limit(limit)

        if not selected_keys and not meta_keys:
            return columns, [], total

        selected_expressions = [
            field_map[key].expression(definition.model).label(key)
            for key in selected_keys
        ]
        selected_expressions.extend(
            field_map[key].expression(definition.model).label(f"__meta_{key}")
            for key in meta_keys
            if key not in selected_keys
        )
        rows = query.with_entities(*selected_expressions).all()

        items: list[dict[str, Any]] = []
        for row in rows:
            item = {
                key: TableConfigurationService._convert_value(getattr(row, key))
                for key in selected_keys
            }
            for key in meta_keys:
                attr = key if key in selected_keys else f"__meta_{key}"
                item[key] = TableConfigurationService._convert_value(getattr(row, attr))
            items.append(item)

        return columns, items, total


def _full_name_expression(model: type) -> Any:
    return func.trim(model.first_name + literal(" ") + model.last_name)


def _activation_state_expression(model: type) -> Any:
    return case((model.is_active.is_(True), literal("active")), else_=literal("inactive"))


def _customer_type_expression(model: type) -> Any:
    return case(
        (model.organization_id.is_not(None), literal("organization")),
        else_=literal("person"),
    )


def _approval_status_expression(model: type) -> Any:
    return case(
        (model.billing_enabled.is_(True), literal("approved")),
        else_=literal("pending"),
    )


def _tier_state_expression(model: type) -> Any:
    return case(
        (model.marketing_opt_in.is_(True), literal("opt_in")),
        else_=literal("standard"),
    )


TableRegistry.register(
    table_key="customers",
    model=Subscriber,
    allowed_fields=[
        "id",
        "email",
        "status",
        "subscriber_number",
        "account_number",
        "first_name",
        "last_name",
        "is_active",
        "user_type",
        "billing_enabled",
        "marketing_opt_in",
        "created_at",
        "updated_at",
        "organization_id",
        "reseller_id",
        "min_balance",
    ],
    computed_fields=[
        TableFieldDefinition(
            key="customer_name",
            label="Customer",
            sortable=True,
            filterable=False,
            expression_resolver=_full_name_expression,
            is_computed=True,
        ),
        TableFieldDefinition(
            key="activation_state",
            label="Activation State",
            sortable=True,
            filterable=True,
            expression_resolver=_activation_state_expression,
            filter_resolver=lambda query, model, value: query.filter(
                model.is_active.is_(str(value).lower() == "active")
            ),
            is_computed=True,
        ),
        TableFieldDefinition(
            key="customer_type",
            label="Customer Type",
            sortable=True,
            filterable=True,
            expression_resolver=_customer_type_expression,
            filter_resolver=lambda query, model, value: query.filter(
                model.organization_id.is_not(None)
                if str(value).lower() == "organization"
                else model.organization_id.is_(None)
            ),
            is_computed=True,
        ),
        TableFieldDefinition(
            key="approval_status",
            label="Approval Status",
            sortable=True,
            filterable=True,
            expression_resolver=_approval_status_expression,
            filter_resolver=lambda query, model, value: query.filter(
                model.billing_enabled.is_(str(value).lower() == "approved")
            ),
            is_computed=True,
        ),
        TableFieldDefinition(
            key="tier_state",
            label="Tier State",
            sortable=True,
            filterable=True,
            expression_resolver=_tier_state_expression,
            filter_resolver=lambda query, model, value: query.filter(
                model.marketing_opt_in.is_(str(value).lower() == "opt_in")
            ),
            is_computed=True,
        ),
    ],
    field_overrides={
        "id": {"label": "Customer ID", "filterable": False},
        "first_name": {"label": "First Name", "hidden_by_default": True},
        "last_name": {"label": "Last Name", "hidden_by_default": True},
        "status": {"label": "Account Status"},
        "is_active": {"label": "Is Active", "hidden_by_default": True},
        "min_balance": {"label": "Balance"},
        "user_type": {"label": "Role/State"},
        "billing_enabled": {"label": "Approval Status"},
        "marketing_opt_in": {"label": "Tier Flag"},
        "organization_id": {"label": "Organization ID", "hidden_by_default": True},
        "reseller_id": {"label": "Reseller ID", "hidden_by_default": True},
    },
    row_meta_fields=["id", "customer_type"],
)

TableRegistry.register(
    table_key="subscribers",
    model=Subscriber,
    allowed_fields=[
        "id",
        "subscriber_number",
        "account_number",
        "email",
        "phone",
        "status",
        "first_name",
        "last_name",
        "is_active",
        "user_type",
        "billing_enabled",
        "marketing_opt_in",
        "organization_id",
        "reseller_id",
        "created_at",
        "updated_at",
    ],
    computed_fields=[
        TableFieldDefinition(
            key="subscriber_name",
            label="Subscriber",
            sortable=True,
            filterable=False,
            expression_resolver=_full_name_expression,
            is_computed=True,
        ),
        TableFieldDefinition(
            key="activation_state",
            label="Activation State",
            sortable=True,
            filterable=True,
            expression_resolver=_activation_state_expression,
            filter_resolver=lambda query, model, value: query.filter(
                model.is_active.is_(str(value).lower() == "active")
            ),
            is_computed=True,
        ),
        TableFieldDefinition(
            key="subscriber_type",
            label="Subscriber Type",
            sortable=True,
            filterable=True,
            expression_resolver=_customer_type_expression,
            filter_resolver=lambda query, model, value: query.filter(
                model.organization_id.is_not(None)
                if str(value).lower() == "organization"
                else model.organization_id.is_(None)
            ),
            is_computed=True,
        ),
        TableFieldDefinition(
            key="approval_status",
            label="Approval Status",
            sortable=True,
            filterable=True,
            expression_resolver=_approval_status_expression,
            filter_resolver=lambda query, model, value: query.filter(
                model.billing_enabled.is_(str(value).lower() == "approved")
            ),
            is_computed=True,
        ),
        TableFieldDefinition(
            key="tier_state",
            label="Tier State",
            sortable=True,
            filterable=True,
            expression_resolver=_tier_state_expression,
            filter_resolver=lambda query, model, value: query.filter(
                model.marketing_opt_in.is_(str(value).lower() == "opt_in")
            ),
            is_computed=True,
        ),
    ],
    field_overrides={
        "id": {"label": "Subscriber ID", "filterable": False},
        "first_name": {"label": "First Name", "hidden_by_default": True},
        "last_name": {"label": "Last Name", "hidden_by_default": True},
        "status": {"label": "Account Status"},
        "is_active": {"label": "Is Active", "hidden_by_default": True},
        "user_type": {"label": "Role/State Flag"},
        "billing_enabled": {"label": "Approval Status"},
        "marketing_opt_in": {"label": "Tier Flag"},
    },
    row_meta_fields=["id"],
)
