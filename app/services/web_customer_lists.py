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
        func.lower(func.coalesce(Subscriber.metadata_["subscriber_category"].as_string(), ""))
        == SubscriberCategory.business.value
    )


def _individual_customer_clause():
    return (
        func.lower(func.coalesce(Subscriber.metadata_["subscriber_category"].as_string(), ""))
        != SubscriberCategory.business.value
    )


def build_contacts_index_context(
    db: Session,
    *,
    search: str | None,
    status: str | None,
    entity_type: str | None,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    """Build contacts list — all contacts are subscribers."""
    offset = (page - 1) * per_page

    _not_deleted = not_(splynx_deleted_import_clause())
    query = db.query(Subscriber).filter(
        Subscriber.user_type != UserType.system_user,
        _not_deleted,
    )

    # Filter by business vs individual
    if entity_type == "business":
        query = query.filter(_business_customer_clause())
    elif entity_type == "individual":
        query = query.filter(_individual_customer_clause())

    if status:
        normalized_status = status.strip().lower()
        if normalized_status in {"active", "customer", "subscriber", "lead", "contact"}:
            query = query.filter(Subscriber.status == SubscriberStatus.active)
        elif normalized_status == "blocked":
            query = query.filter(Subscriber.status == SubscriberStatus.blocked)
        elif normalized_status in {"inactive", "suspended"}:
            query = query.filter(Subscriber.status == SubscriberStatus.suspended)
        elif normalized_status == "disabled":
            query = query.filter(Subscriber.status == SubscriberStatus.disabled)
        elif normalized_status == "new":
            query = query.filter(Subscriber.status == SubscriberStatus.new)
        elif normalized_status in {"delinquent", "canceled"}:
            query = query.filter(
                Subscriber.status == SubscriberStatus(normalized_status)
            )

    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            (Subscriber.first_name.ilike(search_filter))
            | (Subscriber.last_name.ilike(search_filter))
            | (Subscriber.email.ilike(search_filter))
            | (Subscriber.phone.ilike(search_filter))
        )

    _base_count = (
        db.query(func.count(Subscriber.id))
        .filter(Subscriber.user_type != UserType.system_user)
        .filter(_not_deleted)
    )
    active_count = (
        _base_count.filter(Subscriber.status == SubscriberStatus.active).scalar() or 0
    )
    blocked_count = (
        _base_count.filter(Subscriber.status == SubscriberStatus.blocked).scalar() or 0
    )
    suspended_count = (
        _base_count.filter(Subscriber.status == SubscriberStatus.suspended).scalar()
        or 0
    )
    disabled_count = (
        _base_count.filter(Subscriber.status == SubscriberStatus.disabled).scalar() or 0
    )
    new_count = (
        _base_count.filter(Subscriber.status == SubscriberStatus.new).scalar() or 0
    )
    delinquent_count = (
        _base_count.filter(Subscriber.status == SubscriberStatus.delinquent).scalar()
        or 0
    )
    canceled_count = (
        _base_count.filter(Subscriber.status == SubscriberStatus.canceled).scalar() or 0
    )

    people = (
        query.order_by(Subscriber.created_at.desc())
        .limit(per_page)
        .offset(offset)
        .all()
    )
    contacts: list[dict[str, Any]] = []
    for person in people:
        contacts.append(
            {
                "id": str(person.id),
                "type": "person",
                "name": f"{person.first_name} {person.last_name}".strip(),
                "email": person.email,
                "phone": person.phone,
                "status": person.status.value if person.status else "active",
                "organization": person.company_name if person.is_business else None,
                "is_active": person.is_active,
                "is_business": person.is_business,
                "created_at": person.created_at,
                "raw": person,
            }
        )

    total = query.count()
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    stats_total = (
        active_count
        + blocked_count
        + suspended_count
        + disabled_count
        + new_count
        + delinquent_count
        + canceled_count
    )

    return {
        "contacts": contacts,
        "search": search or "",
        "status": status or "",
        "entity_type": entity_type or "",
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "stats": {
            "active": active_count,
            "blocked": blocked_count,
            "suspended": suspended_count,
            "disabled": disabled_count,
            "new": new_count,
            "delinquent": delinquent_count,
            "canceled": canceled_count,
            "customers": active_count,
            "subscribers": active_count,
            "total": stats_total,
        },
    }


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

    _not_deleted2 = not_(splynx_deleted_import_clause())

    # Build status filter clause
    _status_filter: Any = None
    if status:
        normalized = status.strip().lower()
        if normalized == "inactive":
            _status_filter = Subscriber.is_active.is_(False)
        elif normalized in (
            "active",
            "blocked",
            "suspended",
            "disabled",
            "canceled",
            "new",
            "delinquent",
        ):
            _status_filter = Subscriber.status == SubscriberStatus(normalized)

    # All customers are subscribers (including org members)
    query = (
        db.query(Subscriber)
        .options(
            selectinload(Subscriber.subscriptions)
            .selectinload(Subscription.provisioning_nas_device)
            .selectinload(NasDevice.pop_site),
        )
        .filter(Subscriber.user_type != UserType.system_user)
        .filter(_not_deleted2)
    )
    # Optional: filter to business vs individual customers
    if customer_type == "business":
        query = query.filter(_business_customer_clause())
    elif customer_type == "individual":
        query = query.filter(_individual_customer_clause())

    if _status_filter is not None:
        query = query.filter(_status_filter)
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

    people = (
        query.order_by(Subscriber.created_at.desc())
        .limit(per_page)
        .offset(offset)
        .all()
    )
    customers: list[dict[str, Any]] = [_build_customer_dict(p) for p in people]

    # Count query (mirrors filters above)
    count_query = (
        db.query(func.count(Subscriber.id))
        .select_from(Subscriber)
        .filter(Subscriber.user_type != UserType.system_user)
        .filter(_not_deleted2)
    )
    if customer_type == "business":
        count_query = count_query.filter(_business_customer_clause())
    elif customer_type == "individual":
        count_query = count_query.filter(_individual_customer_clause())
    if _status_filter is not None:
        count_query = count_query.filter(_status_filter)
    if search:
        like = f"%{search}%"
        count_query = count_query.filter(
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
        count_query = count_query.filter(
            Subscriber.subscriptions.any(
                Subscription.provisioning_nas_device_id == nas_id
            )
        )
    if pop_site_id:
        count_query = count_query.filter(
            Subscriber.subscriptions.any(
                Subscription.provisioning_nas_device.has(
                    NasDevice.pop_site_id == pop_site_id
                )
            )
        )
    total = count_query.scalar() or 0
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

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
    }
