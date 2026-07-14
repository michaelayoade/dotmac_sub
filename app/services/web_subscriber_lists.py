"""Canonical query and page projection for subscriber list transports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import func
from sqlalchemy.orm import Query, Session

from app.models.subscriber import Subscriber
from app.services import subscriber as subscriber_service
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
    SortDirection,
)

SubscriberListSort = Literal[
    "created_at",
    "updated_at",
    "name",
    "status",
    "subscriber_number",
]

_SUBSCRIBER_STATUS_FILTERS = frozenset(
    {
        "active",
        "blocked",
        "suspended",
        "disabled",
        "canceled",
        "new",
        "delinquent",
        "inactive",
    }
)

SUBSCRIBER_LIST_DEFINITION = ListDefinition(
    key="subscribers",
    fields=(
        ListFieldDefinition("name", "Subscriber", searchable=True, sortable=True),
        ListFieldDefinition("email", "Email", searchable=True),
        ListFieldDefinition("phone", "Phone", searchable=True),
        ListFieldDefinition(
            "subscriber_number",
            "Subscriber number",
            searchable=True,
            sortable=True,
        ),
        ListFieldDefinition("account_number", "Account", searchable=True),
        ListFieldDefinition("network_identity", "Network identity", searchable=True),
        ListFieldDefinition("subscriber_type", "Subscriber type", filterable=True),
        ListFieldDefinition("status", "Status", filterable=True, sortable=True),
        ListFieldDefinition("created_at", "Created", sortable=True),
        ListFieldDefinition("updated_at", "Updated", sortable=True),
    ),
    default_sort="created_at",
    default_sort_dir="desc",
)

_LEGACY_SUBSCRIBER_TABLE_PARAMS = frozenset(
    {
        "_ts",
        "activation_state",
        "limit",
        "offset",
        "q",
        "search",
        "sort_by",
        "sort_dir",
        "status",
        "subscriber_type",
        "table_key",
    }
)
SUBSCRIBER_TABLE_SORT_ALIASES: dict[str, SubscriberListSort] = {
    "created_at": "created_at",
    "updated_at": "updated_at",
    "subscriber_name": "name",
    "name": "name",
    "status": "status",
    "subscriber_number": "subscriber_number",
}


@dataclass(frozen=True, slots=True)
class SubscriberListPage:
    """One normalized and stably ordered subscriber page."""

    query: Query
    list_query: ListQuery
    page_meta: PageMeta


def _normalize_subscriber_type(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"individual", "person"}:
        return "person"
    if normalized == "business":
        return "business"
    return None


def build_subscriber_list_query(
    *,
    search: str | None,
    status: str | None,
    subscriber_type: str | None,
    sort_by: SubscriberListSort = "created_at",
    sort_dir: SortDirection = "desc",
    page: int = 1,
    per_page: int = 25,
) -> ListQuery:
    """Normalize transport inputs through declared subscriber capabilities."""

    raw_type = str(subscriber_type or "").strip()
    normalized_type = _normalize_subscriber_type(raw_type)
    if raw_type and normalized_type is None:
        raise ValueError(f"Unsupported subscriber_type filter: {raw_type}")

    normalized_status = str(status or "").strip().lower() or None
    if normalized_status and normalized_status not in _SUBSCRIBER_STATUS_FILTERS:
        raise ValueError(f"Unsupported status filter: {normalized_status}")

    return SUBSCRIBER_LIST_DEFINITION.build_query(
        search=search,
        filters={
            "subscriber_type": normalized_type,
            "status": normalized_status,
        },
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    )


def build_subscriber_list_query_from_legacy_params(
    request_params: Mapping[str, Any],
) -> ListQuery:
    """Translate the legacy offset table API onto the subscriber list owner."""

    unsupported = sorted(
        key
        for key, value in request_params.items()
        if key not in _LEGACY_SUBSCRIBER_TABLE_PARAMS and str(value or "").strip()
    )
    if unsupported:
        raise ValueError(
            "Unsupported subscriber list parameters: " + ", ".join(unsupported)
        )

    try:
        limit = int(request_params.get("limit", 50) or 50)
        offset = int(request_params.get("offset", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit and offset must be integers") from exc

    if limit not in SUBSCRIBER_LIST_DEFINITION.per_page_options:
        allowed = ", ".join(
            str(size) for size in SUBSCRIBER_LIST_DEFINITION.per_page_options
        )
        raise ValueError(f"limit must be one of: {allowed}")
    if offset < 0:
        raise ValueError("offset must be at least 0")
    if offset % limit:
        raise ValueError("offset must align to the requested limit")

    legacy_sort = str(request_params.get("sort_by") or "created_at").strip()
    sort_by = SUBSCRIBER_TABLE_SORT_ALIASES.get(legacy_sort)
    if sort_by is None:
        raise ValueError(f"Unsupported sort field for subscribers: {legacy_sort}")

    status = str(request_params.get("status") or "").strip()
    activation_state = str(request_params.get("activation_state") or "").strip()
    if status and activation_state and status.lower() != activation_state.lower():
        raise ValueError("status and activation_state filters conflict")

    raw_sort_dir = str(request_params.get("sort_dir") or "desc").strip().lower()
    if raw_sort_dir not in {"asc", "desc"}:
        raise ValueError("sort_dir must be asc or desc")

    return build_subscriber_list_query(
        search=str(
            request_params.get("q") or request_params.get("search") or ""
        ).strip(),
        status=status or activation_state,
        subscriber_type=str(request_params.get("subscriber_type") or "").strip(),
        sort_by=sort_by,
        sort_dir=cast(SortDirection, raw_sort_dir),
        page=(offset // limit) + 1,
        per_page=limit,
    )


def _subscriber_name_sort_expression():
    return func.lower(
        func.coalesce(
            func.nullif(func.trim(Subscriber.company_name), ""),
            func.nullif(func.trim(Subscriber.display_name), ""),
            func.nullif(func.trim(Subscriber.legal_name), ""),
            func.nullif(func.trim(Subscriber.last_name), ""),
            func.nullif(func.trim(Subscriber.first_name), ""),
            Subscriber.email,
            "",
        )
    )


def _apply_subscriber_sort(query: Query, list_query: ListQuery) -> Query:
    if list_query.sort_by == "name":
        expression = _subscriber_name_sort_expression()
    elif list_query.sort_by == "status":
        expression = Subscriber.status
    elif list_query.sort_by == "subscriber_number":
        expression = func.lower(func.coalesce(Subscriber.subscriber_number, ""))
    elif list_query.sort_by == "updated_at":
        expression = Subscriber.updated_at
    else:
        expression = Subscriber.created_at

    ordered = expression.asc() if list_query.sort_dir == "asc" else expression.desc()
    return query.order_by(ordered, Subscriber.id.asc())


def build_subscriber_list_page(
    db: Session,
    *,
    list_query: ListQuery,
) -> SubscriberListPage:
    """Apply shared subscriber scope, count, page clamping, and stable sort."""

    if list_query.definition.key != SUBSCRIBER_LIST_DEFINITION.key:
        raise ValueError("Subscriber list page requires the subscribers definition")

    query = subscriber_service.subscribers.query(
        db,
        subscriber_type=list_query.filter_value("subscriber_type"),
        status=list_query.filter_value("status"),
        search=list_query.search,
        include_deleted=False,
        include_related=False,
    )
    total = query.order_by(None).count()
    page_meta = PageMeta.from_query(list_query, total)
    effective_query = list_query.with_page(page_meta.page)
    page_query = (
        _apply_subscriber_sort(query, effective_query)
        .limit(effective_query.per_page)
        .offset(effective_query.offset)
    )
    return SubscriberListPage(
        query=page_query,
        list_query=effective_query,
        page_meta=page_meta,
    )
