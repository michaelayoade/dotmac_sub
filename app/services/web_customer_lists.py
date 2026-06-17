"""Service helpers for web/admin customer listing routes."""

from __future__ import annotations

import logging
from ipaddress import IPv4Address as ParsedIPv4Address
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

from sqlalchemy import and_, func, not_, or_
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, OntAssignment, OntUnit
from app.models.network_monitoring import PopSite
from app.models.subscriber import (
    Subscriber,
    SubscriberCategory,
    SubscriberStatus,
    UserType,
)
from app.services.subscriber import splynx_deleted_import_clause

_SUBSCRIBER_CATEGORY_COL: Any = Subscriber.metadata_["subscriber_category"].as_string()
_UNSPECIFIED_IPV4 = ParsedIPv4Address(0)


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


def _parse_ipv4_search(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        parsed = ParsedIPv4Address(normalized)
    except ValueError:
        return None
    if parsed == _UNSPECIFIED_IPV4:
        return None
    return str(parsed)


def _valid_ipv4_text(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = ParsedIPv4Address(text)
    except ValueError:
        return None
    if parsed == _UNSPECIFIED_IPV4:
        return None
    return str(parsed)


def _active_subscription_clause():
    return Subscription.status == SubscriptionStatus.active


def _active_ipam_ipv4_match(ip_address: str):
    return (
        Subscriber.ip_assignments.any(
            and_(
                IPAssignment.is_active.is_(True),
                IPAssignment.ipv4_address.has(IPv4Address.address == ip_address),
                or_(
                    IPAssignment.subscription.has(_active_subscription_clause()),
                    and_(
                        IPAssignment.subscription_id.is_(None),
                        Subscriber.subscriptions.any(_active_subscription_clause()),
                    ),
                ),
            )
        )
    )


def _active_ont_ipv4_match(ip_address: str):
    return Subscriber.ont_assignments.any(
        and_(
            OntAssignment.active.is_(True),
            or_(
                OntAssignment.static_ip == ip_address,
                OntAssignment.ont_unit.has(OntUnit.observed_wan_ip == ip_address),
            ),
        )
    )


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
    pppoe_login = None
    ipv4 = None
    ipv4_label = None
    nas_name = None
    pop_site_name = None
    active_subscriptions = [
        sub
        for sub in (person.subscriptions or [])
        if sub.status == SubscriptionStatus.active
    ]
    active_subscription_ids = {sub.id for sub in active_subscriptions}
    active_ipam_assignments = sorted(
        (
            assignment
            for assignment in (person.ip_assignments or [])
            if assignment.is_active
            and assignment.ipv4_address
            and (
                (
                    assignment.subscription_id is not None
                    and assignment.subscription_id in active_subscription_ids
                )
                or (
                    assignment.subscription_id is None
                    and bool(active_subscriptions)
                )
            )
        ),
        key=lambda assignment: assignment.created_at,
        reverse=True,
    )
    if active_ipam_assignments:
        ipv4 = _valid_ipv4_text(active_ipam_assignments[0].ipv4_address.address)
        ipv4_label = "Current IPAM IPv4"

    for sub in active_subscriptions:
        if sub.login:
            pppoe_login = sub.login
        if not ipv4 and sub.ipv4_address:
            service_ipv4 = _valid_ipv4_text(sub.ipv4_address)
            if service_ipv4:
                ipv4 = service_ipv4
                ipv4_label = "Service IPv4"
        if sub.provisioning_nas_device:
            nas_name = sub.provisioning_nas_device.name
            if getattr(sub.provisioning_nas_device, "pop_site", None):
                pop_site_name = sub.provisioning_nas_device.pop_site.name
        if pppoe_login:
            break

    if not ipv4:
        active_ont_assignments = [
            assignment
            for assignment in (person.ont_assignments or [])
            if assignment.active
        ]
        for assignment in active_ont_assignments:
            static_ip = _valid_ipv4_text(assignment.static_ip)
            if static_ip:
                ipv4 = static_ip
                ipv4_label = "ONT WAN IPv4"
                break
            observed_ip = _valid_ipv4_text(
                getattr(assignment.ont_unit, "observed_wan_ip", None)
            )
            if observed_ip:
                ipv4 = observed_ip
                ipv4_label = "Observed ONT WAN IPv4"
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
        "ipv4_label": ipv4_label,
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
        exact_ipv4 = _parse_ipv4_search(search)
        if exact_ipv4:
            query = query.filter(
                or_(
                    Subscriber.subscriptions.any(
                        and_(
                            _active_subscription_clause(),
                            Subscription.ipv4_address == exact_ipv4,
                        )
                    ),
                    _active_ipam_ipv4_match(exact_ipv4),
                    _active_ont_ipv4_match(exact_ipv4),
                )
            )
        else:
            like = f"%{search}%"
            query = query.filter(
                Subscriber.first_name.ilike(like)
                | Subscriber.last_name.ilike(like)
                | Subscriber.display_name.ilike(like)
                | Subscriber.email.ilike(like)
                | Subscriber.phone.ilike(like)
                | Subscriber.subscriber_number.ilike(like)
                | Subscriber.account_number.ilike(like)
                | Subscriber.subscriptions.any(
                    and_(_active_subscription_clause(), Subscription.login.ilike(like))
                )
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
            selectinload(Subscriber.ip_assignments).selectinload(
                IPAssignment.ipv4_address
            ),
            selectinload(Subscriber.ont_assignments).selectinload(
                OntAssignment.ont_unit
            ),
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
