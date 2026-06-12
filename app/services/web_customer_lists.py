"""Service helpers for web/admin customer listing routes."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

from sqlalchemy import func, not_
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import NasDevice, Subscription
from app.models.network_monitoring import PopSite
from app.models.subscriber import (
    Subscriber,
    SubscriberCategory,
    SubscriberStatus,
    UserType,
)
from app.services.subscriber import splynx_deleted_import_clause

_SUBSCRIBER_CATEGORY_COL: Any = Subscriber.metadata_["subscriber_category"].as_string()


def _customer_user_clause():
    return Subscriber.user_type == UserType.customer


def _looks_like_uuid(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if not normalized:
        return False
    try:
        return str(UUID(normalized)) == normalized.lower()
    except (ValueError, AttributeError, TypeError):
        return False


def _customer_display_identifier(*values: str | None) -> str | None:
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or _looks_like_uuid(normalized):
            continue
        return normalized
    return None


def _business_customer_clause():
    return (
        (
            func.coalesce(_SUBSCRIBER_CATEGORY_COL, "")
            == SubscriberCategory.business.value
        )
        | (func.trim(func.coalesce(Subscriber.company_name, "")) != "")
        | (func.trim(func.coalesce(Subscriber.legal_name, "")) != "")
    )


def _individual_customer_clause():
    return not_(_business_customer_clause())


def _build_customer_dict(person: Subscriber) -> dict[str, Any]:
    """Build a customer dict from a subscriber, including subscription info."""
    # Get first active subscription's login/IP/NAS
    pppoe_login = None
    ipv4 = None
    nas_name = None
    pop_site_name = None
    for sub in person.subscriptions or []:
        if sub.login:
            pppoe_login = sub.login
        if sub.ipv4_address:
            ipv4 = sub.ipv4_address
        if sub.provisioning_nas_device:
            nas_name = sub.provisioning_nas_device.name
            if getattr(sub.provisioning_nas_device, "pop_site", None):
                pop_site_name = sub.provisioning_nas_device.pop_site.name
        if pppoe_login:
            break

    display_name = person.company_name or person.display_name or person.full_name
    return {
        "id": str(person.id),
        "type": "business" if person.is_business else "person",
        "name": display_name,
        "subscriber_number": person.subscriber_number,
        "account_number": person.account_number,
        "account_label": _customer_display_identifier(
            person.account_number,
            person.subscriber_number,
        ),
        "display_identifier": _customer_display_identifier(
            person.subscriber_number,
            person.account_number,
            pppoe_login,
        ),
        "pppoe_login": pppoe_login,
        "ipv4": ipv4,
        "nas_name": nas_name,
        "pop_site_name": pop_site_name,
        "email": person.email,
        "phone": person.phone,
        "is_active": person.is_active,
        "is_business": person.is_business,
        "business_name": person.legal_name if person.is_business else None,
        "created_at": person.created_at,
        "raw": person,
    }


def _normalize_customer_type(customer_type: str | None) -> str | None:
    normalized = (customer_type or "").strip().lower()
    if normalized in {"individual", "person"}:
        return "person"
    if normalized == "business":
        return "business"
    return None


def _status_filter_clause(status: str | None) -> Any:
    normalized = (status or "").strip().lower()
    if not normalized:
        return None
    if normalized == "inactive":
        return Subscriber.is_active.is_(False)
    if normalized in (
        "active",
        "blocked",
        "suspended",
        "disabled",
        "canceled",
        "new",
        "delinquent",
    ):
        return Subscriber.status == SubscriberStatus(normalized)
    return None


def _apply_customer_filters(
    query,
    *,
    search: str | None,
    status: str | None,
    customer_type: str | None,
    nas_id: str | None,
    pop_site_id: str | None,
):
    normalized_customer_type = _normalize_customer_type(customer_type)
    status_filter = _status_filter_clause(status)

    if normalized_customer_type == "business":
        query = query.filter(_business_customer_clause())
    elif normalized_customer_type == "person":
        query = query.filter(_individual_customer_clause())

    if status_filter is not None:
        query = query.filter(status_filter)
    if search:
        like = f"%{search}%"
        query = query.filter(
            Subscriber.first_name.ilike(like)
            | Subscriber.last_name.ilike(like)
            | Subscriber.display_name.ilike(like)
            | Subscriber.email.ilike(like)
            | Subscriber.phone.ilike(like)
            | Subscriber.subscriber_number.ilike(like)
            | Subscriber.account_number.ilike(like)
            | Subscriber.subscriptions.any(Subscription.login.ilike(like))
            | Subscriber.subscriptions.any(Subscription.ipv4_address.ilike(like))
        )
    if nas_id:
        query = query.filter(
            Subscriber.subscriptions.any(
                Subscription.provisioning_nas_device_id == nas_id
            )
        )
    if pop_site_id:
        query = query.filter(
            Subscriber.subscriptions.any(
                Subscription.provisioning_nas_device.has(
                    NasDevice.pop_site_id == pop_site_id
                )
            )
        )
    return query


def customer_scope_query(
    db: Session,
    *,
    search: str | None,
    status: str | None,
    customer_type: str | None,
    nas_id: str | None,
    pop_site_id: str | None,
    include_related: bool = True,
):
    query = db.query(Subscriber)
    if include_related:
        query = query.options(
            selectinload(Subscriber.subscriptions)
            .selectinload(Subscription.provisioning_nas_device)
            .selectinload(NasDevice.pop_site),
            selectinload(Subscriber.channels),
        )
    query = query.filter(_customer_user_clause()).filter(
        not_(splynx_deleted_import_clause())
    )
    return _apply_customer_filters(
        query,
        search=search,
        status=status,
        customer_type=customer_type,
        nas_id=nas_id,
        pop_site_id=pop_site_id,
    )


def list_customers_for_scope(
    db: Session,
    *,
    search: str | None,
    status: str | None,
    customer_type: str | None,
    nas_id: str | None,
    pop_site_id: str | None,
) -> list[Subscriber]:
    return (
        customer_scope_query(
            db,
            search=search,
            status=status,
            customer_type=customer_type,
            nas_id=nas_id,
            pop_site_id=pop_site_id,
            include_related=True,
        )
        .order_by(Subscriber.created_at.desc())
        .all()
    )


def active_customer_filter_count(
    *,
    search: str | None,
    status: str | None,
    customer_type: str | None,
    nas_id: str | None,
    pop_site_id: str | None,
) -> int:
    return sum(
        1
        for value in (search, status, customer_type, nas_id, pop_site_id)
        if str(value or "").strip()
    )


def build_customers_index_context(
    db: Session,
    *,
    search: str | None,
    status: str | None = None,
    customer_type: str | None,
    nas_id: str | None = None,
    pop_site_id: str | None = None,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    """Build customer list context — all customers are subscribers."""
    offset = (page - 1) * per_page
    query = customer_scope_query(
        db,
        search=search,
        status=status,
        customer_type=customer_type,
        nas_id=nas_id,
        pop_site_id=pop_site_id,
        include_related=True,
    )

    people = (
        query.order_by(Subscriber.created_at.desc())
        .limit(per_page)
        .offset(offset)
        .all()
    )
    customers: list[dict[str, Any]] = [_build_customer_dict(p) for p in people]

    total = (
        customer_scope_query(
            db,
            search=search,
            status=status,
            customer_type=customer_type,
            nas_id=nas_id,
            pop_site_id=pop_site_id,
            include_related=False,
        )
        .order_by(None)
        .count()
    )
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    business_clause = _business_customer_clause()
    stats_row = (
        db.query(
            func.count().filter(business_clause).label("businesses"),
            func.count().filter(not_(business_clause)).label("people"),
        )
        .filter(_customer_user_clause())
        .filter(not_(splynx_deleted_import_clause()))
        .one()
    )
    total_businesses = int(stats_row.businesses or 0)
    total_people = int(stats_row.people or 0)

    # Load filter dropdown options
    nas_options = (
        db.query(NasDevice.id, NasDevice.name)
        .filter(NasDevice.is_active.is_(True))
        .order_by(NasDevice.name)
        .all()
    )
    pop_site_options = db.query(PopSite.id, PopSite.name).order_by(PopSite.name).all()

    return {
        "customers": customers,
        "stats": {
            "total_customers": total,
            "total_people": total_people,
            "total_organizations": total_businesses,
        },
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "search": search,
        "status": status or "",
        "customer_type": customer_type,
        "nas_id": nas_id or "",
        "pop_site_id": pop_site_id or "",
        "nas_options": nas_options,
        "pop_site_options": pop_site_options,
        "active_filter_count": active_customer_filter_count(
            search=search,
            status=status,
            customer_type=customer_type,
            nas_id=nas_id,
            pop_site_id=pop_site_id,
        ),
    }
