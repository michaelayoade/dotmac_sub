from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.orm import Query, Session

from app.models.catalog import CatalogOffer, NasDevice, Subscription, SubscriptionStatus
from app.models.network_monitoring import PopSite
from app.models.subscriber import (
    Reseller,
    Subscriber,
    SubscriberCategory,
)
from app.models.table_column_config import TableColumnConfig
from app.models.table_column_default_config import TableColumnDefaultConfig
from app.schemas.table_config import (
    TableColumnAvailable,
    TableColumnPreference,
    TableColumnResolved,
)
from app.services import web_customer_lists, web_subscriber_lists

logger = logging.getLogger(__name__)

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
        raise HTTPException(
            status_code=400, detail=f"Field has no expression mapping: {self.key}"
        )


@dataclass(frozen=True)
class TableDefinition:
    table_key: str
    model: type
    fields: tuple[TableFieldDefinition, ...]
    row_meta_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TableDataProjection:
    """Transport-ready table projection with effective pagination metadata."""

    columns: list[TableColumnResolved]
    items: list[dict[str, Any]]
    count: int
    limit: int
    offset: int


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
                raise ValueError(
                    f"Field {field_key} is not present on model {model.__name__}"
                )

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
    def _column_is_sortable(table_key: str, column_key: str) -> bool:
        if table_key == "customers":
            return column_key in web_customer_lists.CUSTOMER_TABLE_SORT_ALIASES
        if table_key == "subscribers":
            return column_key in web_subscriber_lists.SUBSCRIBER_TABLE_SORT_ALIASES
        return False

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
    def get_columns(
        db: Session, user_id: UUID, table_key: str
    ) -> list[TableColumnResolved]:
        definition = TableRegistry.get(table_key)
        state = TableConfigurationService._default_state(definition)

        default_configs = TableConfigurationService._system_default_configs(
            db, table_key
        )
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
                    "account_number",
                    "pppoe_login",
                    "ipv4_address",
                    "nas_name",
                    "status",
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
                    "subscription_name",
                    "status",
                    "reseller_name",
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
                sortable=TableConfigurationService._column_is_sortable(
                    table_key, field.key
                ),
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
                raise HTTPException(
                    status_code=400, detail=f"Invalid column_key: {item.column_key}"
                )
            if item.column_key in seen:
                raise HTTPException(
                    status_code=400, detail=f"Duplicate column_key: {item.column_key}"
                )
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
                    config["display_order"] = (
                        max_specified_order + 1 + config["display_order"]
                    )

        if not any(item["is_visible"] for item in state.values()):
            raise HTTPException(
                status_code=400, detail="At least one column must be visible"
            )

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
                sortable=TableConfigurationService._column_is_sortable(
                    table_key, field.key
                ),
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
                sortable=TableConfigurationService._column_is_sortable(
                    table_key, field.key
                ),
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
                raise HTTPException(
                    status_code=400, detail=f"Invalid column_key: {item.column_key}"
                )
            if item.column_key in seen:
                raise HTTPException(
                    status_code=400, detail=f"Duplicate column_key: {item.column_key}"
                )
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
                    config["display_order"] = (
                        max_specified_order + 1 + config["display_order"]
                    )

        if not any(item["is_visible"] for item in state.values()):
            raise HTTPException(
                status_code=400, detail="At least one column must be visible"
            )

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
    def _build_customer_data_projection(
        db: Session,
        user_id: UUID,
        request_params: dict[str, Any],
    ) -> TableDataProjection:
        """Project configurable columns over the canonical customer-list owner."""

        definition = TableRegistry.get("customers")
        columns = TableConfigurationService.get_columns(db, user_id, "customers")

        try:
            list_query = (
                web_customer_lists.build_customer_list_query_from_legacy_params(
                    request_params
                )
            )
            page = web_customer_lists.build_customer_list_page(
                db,
                list_query=list_query,
                include_related=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return TableConfigurationService._serialize_list_page(
            definition,
            columns,
            page,
        )

    @staticmethod
    def _build_subscriber_data_projection(
        db: Session,
        user_id: UUID,
        request_params: dict[str, Any],
    ) -> TableDataProjection:
        """Project configurable columns over the canonical subscriber owner."""

        definition = TableRegistry.get("subscribers")
        columns = TableConfigurationService.get_columns(db, user_id, "subscribers")
        try:
            list_query = (
                web_subscriber_lists.build_subscriber_list_query_from_legacy_params(
                    request_params
                )
            )
            page = web_subscriber_lists.build_subscriber_list_page(
                db,
                list_query=list_query,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return TableConfigurationService._serialize_list_page(
            definition,
            columns,
            page,
        )

    @staticmethod
    def _serialize_list_page(
        definition: TableDefinition,
        columns: list[TableColumnResolved],
        page: Any,
    ) -> TableDataProjection:
        """Apply column preferences after a resource owner selects its page."""

        field_map = TableConfigurationService._field_map(definition)
        visible_columns = [column for column in columns if column.is_visible]
        selected_keys = [column.column_key for column in visible_columns]
        meta_keys = [key for key in definition.row_meta_fields if key in field_map]

        if not selected_keys and not meta_keys:
            return TableDataProjection(
                columns=columns,
                items=[],
                count=page.page_meta.total_items,
                limit=page.list_query.per_page,
                offset=page.list_query.offset,
            )

        selected_expressions = [
            field_map[key].expression(definition.model).label(key)
            for key in selected_keys
        ]
        selected_expressions.extend(
            field_map[key].expression(definition.model).label(f"__meta_{key}")
            for key in meta_keys
            if key not in selected_keys
        )
        rows = page.query.with_entities(*selected_expressions).all()

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

        return TableDataProjection(
            columns=columns,
            items=items,
            count=page.page_meta.total_items,
            limit=page.list_query.per_page,
            offset=page.list_query.offset,
        )

    @staticmethod
    def build_data_projection(
        db: Session,
        user_id: UUID,
        table_key: str,
        request_params: dict[str, Any],
    ) -> TableDataProjection:
        """Build table data while preserving each resource's named query owner."""

        if table_key == "customers":
            return TableConfigurationService._build_customer_data_projection(
                db,
                user_id,
                request_params,
            )
        if table_key == "subscribers":
            return TableConfigurationService._build_subscriber_data_projection(
                db,
                user_id,
                request_params,
            )
        raise HTTPException(
            status_code=400,
            detail=f"No list projection owner is registered for table: {table_key}",
        )

    @staticmethod
    def apply_query_config(
        db: Session,
        user_id: UUID,
        table_key: str,
        request_params: dict[str, Any],
    ) -> tuple[list[TableColumnResolved], list[dict[str, Any]], int]:
        projection = TableConfigurationService.build_data_projection(
            db,
            user_id,
            table_key,
            request_params,
        )
        return projection.columns, projection.items, projection.count


def _full_name_expression(model: type) -> Any:
    person_name = func.trim(model.first_name + literal(" ") + model.last_name)
    return case(
        (
            _is_business_expression(model),
            func.coalesce(model.company_name, model.display_name, person_name),
        ),
        else_=person_name,
    )


def _category_value_expression(model: type) -> Any:
    return func.lower(
        func.coalesce(model.metadata_["subscriber_category"].as_string(), "")
    )


def _is_business_expression(model: type) -> Any:
    return or_(
        _category_value_expression(model) == SubscriberCategory.business.value,
        func.trim(func.coalesce(model.company_name, "")) != "",
        func.trim(func.coalesce(model.legal_name, "")) != "",
    )


def _activation_state_expression(model: type) -> Any:
    return case(
        (model.is_active.is_(True), literal("active")), else_=literal("inactive")
    )


def _customer_type_expression(model: type) -> Any:
    return case(
        (_is_business_expression(model), literal("business")),
        else_=literal("person"),
    )


def _business_account_id_expression(model: type) -> Any:
    return case((_is_business_expression(model), model.id), else_=literal(None))


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


def _reseller_name_expression(model: type) -> Any:
    return (
        select(Reseller.name)
        .where(Reseller.id == model.reseller_id)
        .correlate(model)
        .scalar_subquery()
    )


def _subscription_name_expression(model: type) -> Any:
    status_rank = case(
        (Subscription.status == SubscriptionStatus.active, 0),
        (Subscription.status == SubscriptionStatus.pending, 1),
        (Subscription.status == SubscriptionStatus.suspended, 2),
        else_=3,
    )
    return (
        select(CatalogOffer.name)
        .select_from(Subscription)
        .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
        .where(Subscription.subscriber_id == model.id)
        .where(
            Subscription.status.in_(
                (
                    SubscriptionStatus.active,
                    SubscriptionStatus.pending,
                    SubscriptionStatus.suspended,
                )
            )
        )
        .order_by(
            status_rank.asc(),
            Subscription.updated_at.desc(),
            Subscription.created_at.desc(),
        )
        .limit(1)
        .correlate(model)
        .scalar_subquery()
    )


def _pppoe_login_expression(model: type) -> Any:
    """First non-null PPPoE login from subscriber's subscriptions."""
    return (
        select(Subscription.login)
        .where(Subscription.subscriber_id == model.id)
        .where(Subscription.login.is_not(None))
        .where(Subscription.login != "")
        .order_by(Subscription.created_at.desc())
        .limit(1)
        .correlate(model)
        .scalar_subquery()
    )


def _ipv4_address_expression(model: type) -> Any:
    """First non-null IPv4 address from subscriber's subscriptions."""
    return (
        select(Subscription.ipv4_address)
        .where(Subscription.subscriber_id == model.id)
        .where(Subscription.ipv4_address.is_not(None))
        .where(Subscription.ipv4_address != "")
        .order_by(Subscription.created_at.desc())
        .limit(1)
        .correlate(model)
        .scalar_subquery()
    )


def _nas_name_expression(model: type) -> Any:
    """NAS device name from subscriber's first subscription with a NAS assignment."""
    return (
        select(NasDevice.name)
        .select_from(Subscription)
        .join(NasDevice, NasDevice.id == Subscription.provisioning_nas_device_id)
        .where(Subscription.subscriber_id == model.id)
        .where(Subscription.provisioning_nas_device_id.is_not(None))
        .order_by(Subscription.created_at.desc())
        .limit(1)
        .correlate(model)
        .scalar_subquery()
    )


def _pop_site_name_expression(model: type) -> Any:
    """POP site name via NAS device from subscriber's subscription."""
    return (
        select(PopSite.name)
        .select_from(Subscription)
        .join(NasDevice, NasDevice.id == Subscription.provisioning_nas_device_id)
        .join(PopSite, PopSite.id == NasDevice.pop_site_id)
        .where(Subscription.subscriber_id == model.id)
        .where(Subscription.provisioning_nas_device_id.is_not(None))
        .where(NasDevice.pop_site_id.is_not(None))
        .order_by(Subscription.created_at.desc())
        .limit(1)
        .correlate(model)
        .scalar_subquery()
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
                _is_business_expression(model)
                if str(value).lower() == "business"
                else ~_is_business_expression(model)
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
        TableFieldDefinition(
            key="pppoe_login",
            label="PPPoE Login",
            sortable=False,
            filterable=False,
            expression_resolver=_pppoe_login_expression,
            is_computed=True,
        ),
        TableFieldDefinition(
            key="ipv4_address",
            label="IP Address",
            sortable=False,
            filterable=False,
            expression_resolver=_ipv4_address_expression,
            is_computed=True,
        ),
        TableFieldDefinition(
            key="nas_name",
            label="NAS Device",
            sortable=False,
            filterable=False,
            expression_resolver=_nas_name_expression,
            is_computed=True,
        ),
        TableFieldDefinition(
            key="pop_site_name",
            label="Location",
            sortable=False,
            filterable=False,
            expression_resolver=_pop_site_name_expression,
            is_computed=True,
        ),
        TableFieldDefinition(
            key="reseller_name",
            label="Reseller",
            sortable=True,
            filterable=False,
            expression_resolver=_reseller_name_expression,
            is_computed=True,
            hidden_by_default=True,
        ),
        TableFieldDefinition(
            key="business_account_id",
            label="Business Account ID",
            sortable=False,
            filterable=False,
            expression_resolver=_business_account_id_expression,
            is_computed=True,
            hidden_by_default=True,
        ),
        TableFieldDefinition(
            key="subscription_name",
            label="Subscription",
            sortable=True,
            filterable=False,
            expression_resolver=_subscription_name_expression,
            is_computed=True,
            hidden_by_default=True,
        ),
    ],
    field_overrides={
        "id": {"label": "Customer ID", "filterable": False, "hidden_by_default": True},
        "first_name": {"label": "First Name", "hidden_by_default": True},
        "last_name": {"label": "Last Name", "hidden_by_default": True},
        "status": {"label": "Account Status"},
        "is_active": {"label": "Is Active", "hidden_by_default": True},
        "min_balance": {"label": "Balance"},
        "user_type": {"label": "Role/State"},
        "billing_enabled": {"label": "Approval Status"},
        "marketing_opt_in": {"label": "Tier Flag"},
        "reseller_id": {"label": "Reseller ID", "hidden_by_default": True},
    },
    row_meta_fields=["id", "customer_type", "business_account_id"],
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
                _is_business_expression(model)
                if str(value).lower() == "business"
                else ~_is_business_expression(model)
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
        TableFieldDefinition(
            key="reseller_name",
            label="Reseller",
            sortable=True,
            filterable=False,
            expression_resolver=_reseller_name_expression,
            is_computed=True,
        ),
        TableFieldDefinition(
            key="business_account_id",
            label="Business Account ID",
            sortable=False,
            filterable=False,
            expression_resolver=_business_account_id_expression,
            is_computed=True,
            hidden_by_default=True,
        ),
        TableFieldDefinition(
            key="subscription_name",
            label="Subscription",
            sortable=True,
            filterable=False,
            expression_resolver=_subscription_name_expression,
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
