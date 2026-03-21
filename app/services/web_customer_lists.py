"""Service helpers for web/admin customer listing routes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

from sqlalchemy import func, not_
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import NasDevice, Subscription
from app.models.network_monitoring import PopSite
from app.models.subscriber import Organization, Subscriber, SubscriberStatus, UserType
from app.services import subscriber as subscriber_service
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


def build_contacts_index_context(
    db: Session,
    *,
    search: str | None,
    status: str | None,
    entity_type: str | None,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    offset = (page - 1) * per_page
    if entity_type == "organization" and status:
        status = None

    _not_deleted = not_(splynx_deleted_import_clause())
    people_query = db.query(Subscriber).filter(
        Subscriber.user_type != UserType.system_user,
        _not_deleted,
    )
    if status:
        normalized_status = status.strip().lower()
        if normalized_status in {"active", "customer", "subscriber", "lead", "contact"}:
            people_query = people_query.filter(Subscriber.status == SubscriberStatus.active)
        elif normalized_status == "blocked":
            people_query = people_query.filter(Subscriber.status == SubscriberStatus.blocked)
        elif normalized_status in {"inactive", "suspended"}:
            people_query = people_query.filter(Subscriber.status == SubscriberStatus.suspended)
        elif normalized_status == "disabled":
            people_query = people_query.filter(Subscriber.status == SubscriberStatus.disabled)
        elif normalized_status == "new":
            people_query = people_query.filter(Subscriber.status == SubscriberStatus.new)
        elif normalized_status in {"delinquent", "canceled"}:
            people_query = people_query.filter(Subscriber.status == SubscriberStatus(normalized_status))

    if search:
        search_filter = f"%{search}%"
        people_query = people_query.filter(
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
    active_people_count = (
        _base_count.filter(Subscriber.status == SubscriberStatus.active).scalar() or 0
    )
    blocked_people_count = (
        _base_count.filter(Subscriber.status == SubscriberStatus.blocked).scalar() or 0
    )
    suspended_people_count = (
        _base_count.filter(Subscriber.status == SubscriberStatus.suspended).scalar() or 0
    )
    disabled_people_count = (
        _base_count.filter(Subscriber.status == SubscriberStatus.disabled).scalar() or 0
    )
    new_people_count = (
        _base_count.filter(Subscriber.status == SubscriberStatus.new).scalar() or 0
    )
    delinquent_people_count = (
        _base_count.filter(Subscriber.status == SubscriberStatus.delinquent).scalar() or 0
    )
    canceled_people_count = (
        _base_count.filter(Subscriber.status == SubscriberStatus.canceled).scalar() or 0
    )
    orgs_count = db.query(func.count(Organization.id)).scalar() or 0

    contacts: list[dict[str, Any]] = []

    if entity_type != "organization":
        people = (
            people_query.order_by(Subscriber.created_at.desc()).limit(per_page).offset(offset).all()
        )
        for person in people:
            contacts.append(
                {
                    "id": str(person.id),
                    "type": "person",
                    "name": f"{person.first_name} {person.last_name}".strip(),
                    "email": person.email,
                    "phone": person.phone,
                    "status": person.status.value if person.status else "active",
                    "organization": person.organization.name if person.organization else None,
                    "is_active": person.is_active,
                    "created_at": person.created_at,
                    "raw": person,
                }
            )

    if entity_type != "person" and not status:
        orgs_query = db.query(Organization)
        if search:
            orgs_query = orgs_query.filter(Organization.name.ilike(f"%{search}%"))
        orgs = orgs_query.order_by(Organization.created_at.desc()).limit(per_page).offset(offset).all()
        for organization in orgs:
            contacts.append(
                {
                    "id": str(organization.id),
                    "type": "organization",
                    "name": organization.name,
                    "email": getattr(organization, "email", None),
                    "phone": getattr(organization, "phone", None),
                    "status": "organization",
                    "organization": None,
                    "is_active": getattr(organization, "is_active", True),
                    "created_at": organization.created_at,
                    "raw": organization,
                }
            )

    contacts.sort(
        key=lambda item: item["created_at"] or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )

    people_total = people_query.count() if entity_type != "organization" else 0
    org_total = 0
    if entity_type != "person" and not status:
        org_query = db.query(func.count(Organization.id))
        if search:
            org_query = org_query.filter(Organization.name.ilike(f"%{search}%"))
        org_total = org_query.scalar() or 0

    total = people_total + org_total
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    stats_total = (
        active_people_count
        + blocked_people_count
        + suspended_people_count
        + disabled_people_count
        + new_people_count
        + delinquent_people_count
        + canceled_people_count
    )
    if entity_type != "person":
        stats_total += orgs_count

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
            "active": active_people_count,
            "blocked": blocked_people_count,
            "suspended": suspended_people_count,
            "disabled": disabled_people_count,
            "new": new_people_count,
            "delinquent": delinquent_people_count,
            "canceled": canceled_people_count,
            "customers": active_people_count,
            "subscribers": active_people_count,
            "organizations": orgs_count,
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
    for sub in (person.subscriptions or []):
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

    return {
        "id": str(person.id),
        "type": "person",
        "name": f"{person.first_name} {person.last_name}",
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
    offset = (page - 1) * per_page
    list_limit = per_page if customer_type else (offset + per_page)
    list_offset = offset if customer_type else 0

    customers: list[dict[str, Any]] = []

    _not_deleted2 = not_(splynx_deleted_import_clause())

    # Build status filter clause
    _status_filter: Any = None
    if status:
        normalized = status.strip().lower()
        if normalized == "inactive":
            _status_filter = Subscriber.is_active.is_(False)
        elif normalized in (
            "active", "blocked", "suspended", "disabled",
            "canceled", "new", "delinquent",
        ):
            _status_filter = Subscriber.status == SubscriberStatus(normalized)

    if customer_type != "organization":
        people_query = (
            db.query(Subscriber)
            .options(
                selectinload(Subscriber.subscriptions)
                .selectinload(Subscription.provisioning_nas_device)
                .selectinload(NasDevice.pop_site),
            )
            .filter(Subscriber.organization_id.is_(None))
            .filter(Subscriber.user_type != UserType.system_user)
            .filter(_not_deleted2)
        )
        if _status_filter is not None:
            people_query = people_query.filter(_status_filter)
        if search:
            like = f"%{search}%"
            people_query = people_query.filter(
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
        # NAS device filter
        if nas_id:
            people_query = people_query.filter(
                Subscriber.subscriptions.any(
                    Subscription.provisioning_nas_device_id == nas_id
                )
            )
        # POP site filter (via NAS device's pop_site)
        if pop_site_id:
            people_query = people_query.filter(
                Subscriber.subscriptions.any(
                    Subscription.provisioning_nas_device.has(
                        NasDevice.pop_site_id == pop_site_id
                    )
                )
            )
        people = (
            people_query.order_by(Subscriber.created_at.desc())
            .limit(list_limit).offset(list_offset).all()
        )
        for person in people:
            customers.append(_build_customer_dict(person))

    if customer_type != "person":
        orgs = subscriber_service.organizations.list(
            db=db,
            name=search if search else None,
            order_by="name",
            order_dir="asc",
            limit=list_limit,
            offset=list_offset,
        )
        for organization in orgs:
            customers.append(
                {
                    "id": str(organization.id),
                    "type": "organization",
                    "name": organization.name,
                    "account_number": getattr(organization, "account_number", None),
                    "account_label": _customer_display_identifier(
                        getattr(organization, "account_number", None),
                        getattr(organization, "customer_number", None),
                    ),
                    "display_identifier": _customer_display_identifier(
                        getattr(organization, "account_number", None),
                        getattr(organization, "customer_number", None),
                    ),
                    "pppoe_login": None,
                    "ipv4": None,
                    "nas_name": None,
                    "pop_site_name": None,
                    "email": getattr(organization, "email", None),
                    "phone": getattr(organization, "phone", None),
                    "is_active": getattr(organization, "is_active", True),
                    "created_at": organization.created_at,
                    "raw": organization,
                }
            )

    customers.sort(key=lambda item: item["created_at"] or "", reverse=True)

    people_total = 0
    org_total = 0
    if customer_type != "organization":
        people_count_query = (
            db.query(func.count(Subscriber.id))
            .select_from(Subscriber)
            .filter(Subscriber.organization_id.is_(None))
            .filter(Subscriber.user_type != UserType.system_user)
            .filter(_not_deleted2)
        )
        if _status_filter is not None:
            people_count_query = people_count_query.filter(_status_filter)
        if search:
            like = f"%{search}%"
            people_count_query = people_count_query.filter(
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
            people_count_query = people_count_query.filter(
                Subscriber.subscriptions.any(
                    Subscription.provisioning_nas_device_id == nas_id
                )
            )
        if pop_site_id:
            people_count_query = people_count_query.filter(
                Subscriber.subscriptions.any(
                    Subscription.provisioning_nas_device.has(
                        NasDevice.pop_site_id == pop_site_id
                    )
                )
            )
        people_total = people_count_query.scalar() or 0

    if customer_type != "person":
        org_query = db.query(func.count(Organization.id))
        if search:
            org_query = org_query.filter(Organization.name.ilike(f"%{search}%"))
        org_total = org_query.scalar() or 0

    total = people_total + org_total
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    if not customer_type:
        customers = customers[offset : offset + per_page]

    # Load filter dropdown options
    nas_options = (
        db.query(NasDevice.id, NasDevice.name)
        .filter(NasDevice.is_active.is_(True))
        .order_by(NasDevice.name)
        .all()
    )
    pop_site_options = (
        db.query(PopSite.id, PopSite.name)
        .order_by(PopSite.name)
        .all()
    )

    return {
        "customers": customers,
        "stats": {
            "total_customers": total,
            "total_people": people_total,
            "total_organizations": org_total,
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
