"""Service helpers for web/admin customer listing routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.subscriber import Organization, Subscriber, SubscriberStatus, UserType
from app.services import subscriber as subscriber_service


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

    people_query = db.query(Subscriber).filter(Subscriber.user_type != UserType.system_user)
    if status:
        normalized_status = status.strip().lower()
        if normalized_status in {"active", "customer", "subscriber", "lead", "contact"}:
            people_query = people_query.filter(Subscriber.status == SubscriberStatus.active)
        elif normalized_status in {"inactive", "suspended"}:
            people_query = people_query.filter(Subscriber.status == SubscriberStatus.suspended)
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

    active_people_count = (
        db.query(func.count(Subscriber.id))
        .filter(Subscriber.status == SubscriberStatus.active)
        .filter(Subscriber.user_type != UserType.system_user)
        .scalar()
        or 0
    )
    suspended_people_count = (
        db.query(func.count(Subscriber.id))
        .filter(Subscriber.status == SubscriberStatus.suspended)
        .filter(Subscriber.user_type != UserType.system_user)
        .scalar()
        or 0
    )
    delinquent_people_count = (
        db.query(func.count(Subscriber.id))
        .filter(Subscriber.status == SubscriberStatus.delinquent)
        .filter(Subscriber.user_type != UserType.system_user)
        .scalar()
        or 0
    )
    canceled_people_count = (
        db.query(func.count(Subscriber.id))
        .filter(Subscriber.status == SubscriberStatus.canceled)
        .filter(Subscriber.user_type != UserType.system_user)
        .scalar()
        or 0
    )
    orgs_count = db.query(func.count(Organization.id)).scalar() or 0

    contacts: list[dict[str, Any]] = []
    list_limit = offset + per_page

    if entity_type != "organization":
        people = (
            people_query.order_by(Subscriber.created_at.desc()).limit(list_limit).offset(0).all()
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
        orgs = orgs_query.order_by(Organization.created_at.desc()).limit(list_limit).offset(0).all()
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
    contacts = contacts[offset : offset + per_page]

    stats_total = (
        active_people_count
        + suspended_people_count
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
            "leads": 0,
            "contacts": 0,
            "customers": active_people_count,
            "subscribers": active_people_count,
            "organizations": orgs_count,
            "total": stats_total,
        },
    }


def build_customers_index_context(
    db: Session,
    *,
    search: str | None,
    customer_type: str | None,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    offset = (page - 1) * per_page
    list_limit = per_page if customer_type else (offset + per_page)
    list_offset = offset if customer_type else 0

    customers: list[dict[str, Any]] = []

    if customer_type != "organization":
        people_query = (
            db.query(Subscriber)
            .filter(Subscriber.organization_id.is_(None))
            .filter(Subscriber.user_type != UserType.system_user)
        )
        if search:
            people_query = people_query.filter(Subscriber.email.ilike(f"%{search}%"))
        people = (
            people_query.order_by(Subscriber.created_at.desc()).limit(list_limit).offset(list_offset).all()
        )
        for person in people:
            customers.append(
                {
                    "id": str(person.id),
                    "type": "person",
                    "name": f"{person.first_name} {person.last_name}",
                    "email": person.email,
                    "phone": person.phone,
                    "is_active": person.is_active,
                    "created_at": person.created_at,
                    "raw": person,
                }
            )

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
        )
        if search:
            people_count_query = people_count_query.filter(Subscriber.email.ilike(f"%{search}%"))
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
        "customer_type": customer_type,
    }
