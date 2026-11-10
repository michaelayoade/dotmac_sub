import logging
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.connector import ConnectorAuthType, ConnectorConfig, ConnectorType
from app.schemas.connector import ConnectorConfigCreate, ConnectorConfigUpdate
from app.services.common import (
    apply_ordering,
    apply_pagination,
    validate_enum,
)
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
)
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


CONNECTOR_LIST_DEFINITION = ListDefinition(
    key="legacy-connectors",
    fields=(
        ListFieldDefinition("name", "Connection", searchable=True, sortable=True),
        ListFieldDefinition("status", "State", filterable=True),
    ),
    default_sort="name",
    default_sort_dir="asc",
)


def connector_list_query(
    *,
    search: str = "",
    status: str = "all",
    sort: str = "name",
    direction: str = "asc",
    page: int = 1,
    per_page: int = 25,
) -> ListQuery:
    return CONNECTOR_LIST_DEFINITION.build_query(
        search=search[:160],
        filters={
            "status": status if status in {"all", "enabled", "disabled"} else "all"
        },
        sort_by=sort if sort in CONNECTOR_LIST_DEFINITION.sortable_keys else "name",
        sort_dir=direction if direction in {"asc", "desc"} else "asc",
        page=max(1, page),
        per_page=per_page
        if per_page in CONNECTOR_LIST_DEFINITION.per_page_options
        else 25,
    )


@dataclass(frozen=True, slots=True)
class ConnectorListPage:
    rows: tuple[ConnectorConfig, ...]
    query: ListQuery
    page_meta: PageMeta


class ConnectorConfigs(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ConnectorConfigCreate):
        config = ConnectorConfig(**payload.model_dump())
        db.add(config)
        db.commit()
        db.refresh(config)
        return config

    @staticmethod
    def get(db: Session, config_id: str):
        config = db.get(ConnectorConfig, config_id)
        if not config:
            raise HTTPException(status_code=404, detail="Connector config not found")
        return config

    @staticmethod
    def list(
        db: Session,
        connector_type: str | None,
        auth_type: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ConnectorConfig)
        if connector_type:
            query = query.filter(
                ConnectorConfig.connector_type
                == validate_enum(connector_type, ConnectorType, "connector_type")
            )
        if auth_type:
            query = query.filter(
                ConnectorConfig.auth_type
                == validate_enum(auth_type, ConnectorAuthType, "auth_type")
            )
        if is_active is None:
            query = query.filter(ConnectorConfig.is_active.is_(True))
        else:
            query = query.filter(ConnectorConfig.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ConnectorConfig.created_at, "name": ConnectorConfig.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_all(
        db: Session,
        connector_type: str | None,
        auth_type: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ConnectorConfig)
        if connector_type:
            query = query.filter(
                ConnectorConfig.connector_type
                == validate_enum(connector_type, ConnectorType, "connector_type")
            )
        if auth_type:
            query = query.filter(
                ConnectorConfig.auth_type
                == validate_enum(auth_type, ConnectorAuthType, "auth_type")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ConnectorConfig.created_at, "name": ConnectorConfig.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_page(db: Session, query: ListQuery) -> ConnectorListPage:
        rows = db.query(ConnectorConfig)
        if query.search:
            rows = rows.filter(ConnectorConfig.name.ilike(f"%{query.search}%"))
        status = query.filter_value("status")
        if status in {"enabled", "disabled"}:
            rows = rows.filter(ConnectorConfig.is_active.is_(status == "enabled"))
        total = rows.count()
        page_meta = PageMeta.from_query(query, total)
        if page_meta.page != query.page:
            query = query.with_page(page_meta.page)
            page_meta = PageMeta.from_query(query, total)
        direction = (
            ConnectorConfig.name.asc
            if query.sort_dir == "asc"
            else ConnectorConfig.name.desc
        )
        tie_breaker = (
            ConnectorConfig.id.asc
            if query.sort_dir == "asc"
            else ConnectorConfig.id.desc
        )
        page = (
            rows.order_by(direction(), tie_breaker())
            .limit(query.per_page)
            .offset(query.offset)
            .all()
        )
        return ConnectorListPage(rows=tuple(page), query=query, page_meta=page_meta)

    @staticmethod
    def update(db: Session, config_id: str, payload: ConnectorConfigUpdate):
        config = db.get(ConnectorConfig, config_id)
        if not config:
            raise HTTPException(status_code=404, detail="Connector config not found")
        data = payload.model_dump(exclude_unset=True)
        if (
            "auth_config" in data
            and data["auth_config"]
            and data.get("auth_type", config.auth_type) == config.auth_type
        ):
            merged = dict(config.auth_config or {})
            merged.update(data["auth_config"])
            data["auth_config"] = merged
        for key, value in data.items():
            setattr(config, key, value)
        db.commit()
        db.refresh(config)
        return config

    @staticmethod
    def delete(db: Session, config_id: str):
        config = db.get(ConnectorConfig, config_id)
        if not config:
            raise HTTPException(status_code=404, detail="Connector config not found")
        config.is_active = False
        db.commit()


connector_configs = ConnectorConfigs()
