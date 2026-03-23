"""Service helpers for admin reseller management routes."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from uuid import UUID

from sqlalchemy import func, inspect, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.auth import AuthProvider
from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.models.catalog import CatalogOffer, Subscription, SubscriptionStatus
from app.models.offer_availability import OfferResellerAvailability
from app.models.rbac import Role
from app.models.subscriber import Reseller, ResellerUser, Subscriber, UserType
from app.models.support import Ticket, TicketStatus
from app.schemas.auth import UserCredentialCreate
from app.schemas.rbac import SubscriberRoleCreate
from app.schemas.subscriber import SubscriberCreate
from app.services import auth as auth_service
from app.services import rbac as rbac_service
from app.services import subscriber as subscriber_service
from app.services import web_system_user_mutations as web_system_user_mutations_service
from app.services.auth_flow import hash_password
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

RESOLVED_TICKET_STATUSES = {
    TicketStatus.resolved,
    TicketStatus.closed,
    TicketStatus.canceled,
    TicketStatus.merged,
}


def _reseller_users_table_available(db: Session) -> bool:
    """Return True when the dedicated reseller link table exists."""
    bind = db.get_bind()
    return bool(inspect(bind).has_table("reseller_users"))


def _link_via_subscriber_fallback(
    db: Session,
    *,
    reseller_id: UUID,
    subscriber_id: UUID,
) -> ResellerUser:
    """Compatibility link path for schemas without reseller_users table."""
    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        raise ValueError("Subscriber not found for reseller linking")
    subscriber.reseller_id = reseller_id
    subscriber.user_type = getattr(
        type(subscriber.user_type), "reseller", subscriber.user_type
    )
    db.flush()
    return cast(
        ResellerUser,
        SimpleNamespace(
            id=subscriber.id,
            reseller_id=reseller_id,
            subscriber_id=subscriber.id,
            person_id=subscriber.id,
            is_active=True,
            created_at=subscriber.created_at,
        ),
    )


def create_subscriber_credential(
    db: Session,
    *,
    first_name: str,
    last_name: str,
    email: str,
    username: str | None = None,
    password: str | None = None,
    require_password_change: bool = True,
) -> Subscriber:
    """Create a subscriber with local auth credentials."""
    subscriber = cast(
        Subscriber,
        subscriber_service.subscribers.create(
            db=db,
            payload=SubscriberCreate(
                first_name=first_name,
                last_name=last_name,
                email=email,
                is_active=True,
            ),
        ),
    )
    credential_payload = UserCredentialCreate(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username=(username or email).strip(),
        password_hash=hash_password(password or secrets.token_urlsafe(24)),
        must_change_password=require_password_change,
        password_updated_at=datetime.now(UTC),
        is_active=True,
    )
    auth_service.user_credentials.create(db=db, payload=credential_payload)
    return subscriber


def get_role_by_name(db: Session, role_name: str) -> Role | None:
    """Look up a role by its name."""
    stmt = select(Role).where(Role.name == role_name)
    return db.scalars(stmt).first()


def create_reseller_user_link(
    db: Session,
    *,
    reseller_id: UUID,
    subscriber_id: UUID,
) -> ResellerUser:
    """Create a ResellerUser link between a reseller and subscriber."""
    if not _reseller_users_table_available(db):
        return _link_via_subscriber_fallback(
            db,
            reseller_id=reseller_id,
            subscriber_id=subscriber_id,
        )

    link = ResellerUser(
        reseller_id=reseller_id,
        subscriber_id=subscriber_id,
        is_active=True,
    )
    db.add(link)
    db.flush()
    return link


def create_reseller_with_user(
    db: Session,
    *,
    reseller: Reseller,
    user_payload: dict[str, str | None],
) -> None:
    """Create a subscriber credential, assign optional role, and link to reseller.

    Commits the transaction on success.
    """
    subscriber = create_subscriber_credential(
        db,
        first_name=user_payload["first_name"] or "",
        last_name=user_payload["last_name"] or "",
        email=user_payload["email"] or "",
        username=user_payload.get("username"),
        password=user_payload.get("password"),
        require_password_change=True,
    )
    subscriber.user_type = getattr(
        type(subscriber.user_type), "reseller", subscriber.user_type
    )
    subscriber.reseller_id = reseller.id
    if user_payload.get("role"):
        role = get_role_by_name(db, user_payload["role"] or "")
        if role:
            rbac_service.subscriber_roles.create(
                db,
                SubscriberRoleCreate(
                    subscriber_id=subscriber.id,
                    role_id=role.id,
                ),
            )
    create_reseller_user_link(
        db,
        reseller_id=reseller.id,
        subscriber_id=subscriber.id,
    )
    db.commit()
    invite_note = send_reseller_portal_invite(db, email=subscriber.email)
    if "could not" in invite_note.lower():
        logger.warning(
            "Reseller invite issue for %s: %s", subscriber.email, invite_note
        )


def list_reseller_subscribers(
    db: Session,
    reseller_id: str,
) -> list[ResellerUser]:
    """List managed subscriber accounts linked to the reseller."""

    subscribers = list(
        db.scalars(
            select(Subscriber)
            .where(Subscriber.reseller_id == coerce_uuid(reseller_id))
            .where(Subscriber.user_type != UserType.reseller)
            .where(Subscriber.user_type != UserType.system_user)
            .order_by(Subscriber.created_at.desc())
        ).all()
    )
    links: list[ResellerUser] = []
    for subscriber in subscribers:
        link = cast(
            ResellerUser,
            SimpleNamespace(
                id=subscriber.id,
                reseller_id=subscriber.reseller_id,
                subscriber_id=subscriber.id,
                person_id=subscriber.id,
                is_active=subscriber.is_active,
                created_at=subscriber.created_at,
                person=subscriber,
            ),
        )
        links.append(link)
    return links


def count_reseller_subscribers(db: Session, reseller_id: str) -> int:
    """Count managed subscriber accounts linked to the reseller."""
    count = db.scalar(
        select(func.count(Subscriber.id))
        .where(Subscriber.reseller_id == coerce_uuid(reseller_id))
        .where(Subscriber.user_type != UserType.reseller)
        .where(Subscriber.user_type != UserType.system_user)
    )
    return int(count or 0)


def count_subscribers_by_reseller_ids(
    db: Session,
    reseller_ids: list[str],
) -> dict[str, int]:
    """Return subscriber counts keyed by reseller_id for list pages."""
    if not reseller_ids:
        return {}
    reseller_uuids = [coerce_uuid(item) for item in reseller_ids]
    rows = db.execute(
        select(
            Subscriber.reseller_id,
            func.count(Subscriber.id),
        )
        .where(Subscriber.reseller_id.in_(reseller_uuids))
        .where(Subscriber.user_type != UserType.reseller)
        .where(Subscriber.user_type != UserType.system_user)
        .group_by(Subscriber.reseller_id)
    ).all()
    counts: dict[str, int] = {str(row[0]): int(row[1]) for row in rows if row[0]}
    return counts


def list_reseller_subscribers_page(
    db: Session,
    reseller_id: str,
    *,
    limit: int,
    offset: int,
) -> list[ResellerUser]:
    """List a page of managed subscriber accounts linked to the reseller."""
    subscribers = list(
        db.scalars(
            select(Subscriber)
            .where(Subscriber.reseller_id == coerce_uuid(reseller_id))
            .where(Subscriber.user_type != UserType.reseller)
            .where(Subscriber.user_type != UserType.system_user)
            .order_by(Subscriber.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
    links: list[ResellerUser] = []
    for subscriber in subscribers:
        link = cast(
            ResellerUser,
            SimpleNamespace(
                id=subscriber.id,
                reseller_id=subscriber.reseller_id,
                subscriber_id=subscriber.id,
                person_id=subscriber.id,
                is_active=subscriber.is_active,
                created_at=subscriber.created_at,
                person=subscriber,
            ),
        )
        links.append(link)
    return links


def list_reseller_people(db: Session) -> list[Subscriber]:
    """List person-type subscribers for linking to resellers."""
    return cast(
        list[Subscriber],
        subscriber_service.subscribers.list(
            db=db,
            business_account_id=None,
            subscriber_type="person",
            order_by="created_at",
            order_dir="asc",
            limit=500,
            offset=0,
        ),
    )


def get_reseller_detail_context(
    db: Session,
    reseller_id: str,
    *,
    page: int = 1,
    per_page: int = 50,
) -> dict | None:
    """Load reseller detail page data: reseller with linked subscribers list.

    Returns None if the reseller is not found.
    """
    reseller = get_reseller_by_id(db, reseller_id)
    if not reseller:
        return None
    reseller_uuid = coerce_uuid(reseller_id)
    linked_subscriber_query = (
        select(Subscriber.id, Subscriber.status)
        .where(Subscriber.reseller_id == reseller_uuid)
        .where(Subscriber.user_type != UserType.reseller)
        .where(Subscriber.user_type != UserType.system_user)
    )
    linked_subscriber_rows = list(db.execute(linked_subscriber_query).all())
    linked_subscriber_ids = [row[0] for row in linked_subscriber_rows]
    total_subscribers = count_reseller_subscribers(db, reseller_id)
    safe_per_page = max(10, min(per_page, 200))
    safe_page = max(1, page)
    total_pages = max(1, (total_subscribers + safe_per_page - 1) // safe_per_page)
    if safe_page > total_pages:
        safe_page = total_pages
    offset = (safe_page - 1) * safe_per_page
    reseller_subscribers = list_reseller_subscribers_page(
        db,
        reseller_id,
        limit=safe_per_page,
        offset=offset,
    )
    subscriber_status_counts: dict[str, int] = {}
    for _, status in linked_subscriber_rows:
        key = getattr(status, "value", str(status))
        subscriber_status_counts[key] = subscriber_status_counts.get(key, 0) + 1

    reseller_portal_users = int(
        db.scalar(
            select(func.count(Subscriber.id))
            .where(Subscriber.reseller_id == reseller_uuid)
            .where(Subscriber.user_type == UserType.reseller)
        )
        or 0
    )

    active_services = 0
    pending_services = 0
    suspended_services = 0
    subscriptions_total = 0
    outstanding_balance = Decimal("0.00")
    overdue_invoices = 0
    recent_invoices: list[Invoice] = []
    recent_payments: list[Payment] = []
    recent_tickets: list[Ticket] = []
    recent_subscriptions: list[Subscription] = []
    explicit_available_offers: list[CatalogOffer] = []
    explicit_available_offers_total = 0
    payments_30d_total = Decimal("0.00")
    payments_30d_count = 0
    open_tickets = 0

    if linked_subscriber_ids:
        subscriptions_total = int(
            db.scalar(
                select(func.count(Subscription.id)).where(
                    Subscription.subscriber_id.in_(linked_subscriber_ids)
                )
            )
            or 0
        )
        active_services = int(
            db.scalar(
                select(func.count(Subscription.id))
                .where(Subscription.subscriber_id.in_(linked_subscriber_ids))
                .where(Subscription.status == SubscriptionStatus.active)
            )
            or 0
        )
        pending_services = int(
            db.scalar(
                select(func.count(Subscription.id))
                .where(Subscription.subscriber_id.in_(linked_subscriber_ids))
                .where(Subscription.status == SubscriptionStatus.pending)
            )
            or 0
        )
        suspended_services = int(
            db.scalar(
                select(func.count(Subscription.id))
                .where(Subscription.subscriber_id.in_(linked_subscriber_ids))
                .where(Subscription.status == SubscriptionStatus.suspended)
            )
            or 0
        )
        recent_subscriptions = list(
            db.query(Subscription)
            .options(
                joinedload(Subscription.offer), joinedload(Subscription.subscriber)
            )
            .filter(Subscription.subscriber_id.in_(linked_subscriber_ids))
            .order_by(Subscription.created_at.desc())
            .limit(5)
            .all()
        )

        outstanding_balance = db.scalar(
            select(func.coalesce(func.sum(Invoice.balance_due), 0))
            .where(Invoice.account_id.in_(linked_subscriber_ids))
            .where(
                Invoice.status.in_(
                    [
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    ]
                )
            )
        ) or Decimal("0.00")
        overdue_invoices = int(
            db.scalar(
                select(func.count(Invoice.id))
                .where(Invoice.account_id.in_(linked_subscriber_ids))
                .where(Invoice.status == InvoiceStatus.overdue)
            )
            or 0
        )
        recent_invoices = list(
            db.query(Invoice)
            .options(joinedload(Invoice.account))
            .filter(Invoice.account_id.in_(linked_subscriber_ids))
            .order_by(Invoice.created_at.desc())
            .limit(5)
            .all()
        )

        payments_since = datetime.now(UTC) - timedelta(days=30)
        payments_30d_total = db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0))
            .where(Payment.account_id.in_(linked_subscriber_ids))
            .where(Payment.status == PaymentStatus.succeeded)
            .where(Payment.created_at >= payments_since)
        ) or Decimal("0.00")
        payments_30d_count = int(
            db.scalar(
                select(func.count(Payment.id))
                .where(Payment.account_id.in_(linked_subscriber_ids))
                .where(Payment.status == PaymentStatus.succeeded)
                .where(Payment.created_at >= payments_since)
            )
            or 0
        )
        recent_payments = list(
            db.query(Payment)
            .options(joinedload(Payment.account))
            .filter(Payment.account_id.in_(linked_subscriber_ids))
            .order_by(Payment.created_at.desc())
            .limit(5)
            .all()
        )

        ticket_scope = or_(
            Ticket.subscriber_id.in_(linked_subscriber_ids),
            Ticket.customer_account_id.in_(linked_subscriber_ids),
            Ticket.customer_person_id.in_(linked_subscriber_ids),
        )
        open_tickets = int(
            db.scalar(
                select(func.count(Ticket.id))
                .where(ticket_scope)
                .where(Ticket.status.notin_(list(RESOLVED_TICKET_STATUSES)))
            )
            or 0
        )
        recent_tickets = list(
            db.query(Ticket)
            .filter(ticket_scope)
            .order_by(Ticket.updated_at.desc())
            .limit(5)
            .all()
        )

    explicit_available_offers_total = int(
        db.scalar(
            select(func.count(OfferResellerAvailability.id))
            .where(OfferResellerAvailability.reseller_id == reseller_uuid)
            .where(OfferResellerAvailability.is_active.is_(True))
        )
        or 0
    )
    explicit_available_offers = list(
        db.query(CatalogOffer)
        .join(
            OfferResellerAvailability,
            OfferResellerAvailability.offer_id == CatalogOffer.id,
        )
        .filter(OfferResellerAvailability.reseller_id == reseller_uuid)
        .filter(OfferResellerAvailability.is_active.is_(True))
        .filter(CatalogOffer.is_active.is_(True))
        .order_by(CatalogOffer.name.asc())
        .limit(8)
        .all()
    )
    return {
        "reseller": reseller,
        "reseller_subscribers": reseller_subscribers,
        "reseller_subscribers_total": total_subscribers,
        "reseller_portal_users": reseller_portal_users,
        "subscriber_status_counts": subscriber_status_counts,
        "active_services": active_services,
        "pending_services": pending_services,
        "suspended_services": suspended_services,
        "subscriptions_total": subscriptions_total,
        "outstanding_balance": outstanding_balance,
        "overdue_invoices": overdue_invoices,
        "payments_30d_total": payments_30d_total,
        "payments_30d_count": payments_30d_count,
        "open_tickets": open_tickets,
        "recent_invoices": recent_invoices,
        "recent_payments": recent_payments,
        "recent_tickets": recent_tickets,
        "recent_subscriptions": recent_subscriptions,
        "explicit_available_offers": explicit_available_offers,
        "explicit_available_offers_total": explicit_available_offers_total,
        "reseller_urls": {
            "billing_overview": f"/admin/billing?partner_id={reseller.id}",
            "invoices": f"/admin/billing/invoices?partner_id={reseller.id}",
            "payments": f"/admin/billing/payments?partner_id={reseller.id}",
            "accounts": f"/admin/billing/accounts?reseller_id={reseller.id}",
            "provisioning": f"/admin/provisioning/migrate?reseller_id={reseller.id}",
            "settings": f"/admin/resellers/{reseller.id}#reseller-details",
            "subscribers": f"/admin/resellers/{reseller.id}#linked-subscribers",
            "support": f"/admin/resellers/{reseller.id}#support-activity",
            "catalog": f"/admin/resellers/{reseller.id}#catalog-access",
            "services": f"/admin/resellers/{reseller.id}#service-activity",
        },
        "page": safe_page,
        "per_page": safe_per_page,
        "total_pages": total_pages,
    }


def link_existing_subscriber_to_reseller(
    db: Session,
    *,
    reseller_id: str,
    subscriber_id: str,
) -> bool:
    """Link an existing subscriber account to a reseller."""
    r_uuid = coerce_uuid(reseller_id)
    s_uuid = coerce_uuid(subscriber_id)

    subscriber = db.get(Subscriber, s_uuid)
    if not subscriber:
        raise ValueError("Subscriber not found.")
    user_type_value = str(getattr(subscriber.user_type, "value", subscriber.user_type))
    if user_type_value in {"system_user", "reseller"}:
        raise ValueError("Only customer subscribers can be linked to a reseller.")
    if subscriber.reseller_id == r_uuid:
        raise ValueError("Subscriber is already linked to this reseller.")
    subscriber.reseller_id = r_uuid
    db.commit()
    return True


def create_and_link_reseller_user(
    db: Session,
    *,
    reseller_id: str,
    first_name: str,
    last_name: str,
    email: str,
    username: str | None = None,
    password: str | None = None,
) -> None:
    """Create a new subscriber with credentials and link to a reseller."""
    subscriber = create_subscriber_credential(
        db,
        first_name=first_name,
        last_name=last_name,
        email=email,
        username=username,
        password=password,
        require_password_change=True,
    )
    reseller_uuid = coerce_uuid(reseller_id)
    subscriber.user_type = getattr(
        type(subscriber.user_type), "reseller", subscriber.user_type
    )
    subscriber.reseller_id = reseller_uuid
    create_reseller_user_link(
        db,
        reseller_id=reseller_uuid,
        subscriber_id=subscriber.id,
    )
    db.commit()
    invite_note = send_reseller_portal_invite(db, email=subscriber.email)
    if "could not" in invite_note.lower():
        logger.warning(
            "Reseller invite issue for %s: %s", subscriber.email, invite_note
        )


def send_reseller_portal_invite(db: Session, *, email: str) -> str:
    """Send a welcome invite with reseller-portal login destination."""
    return web_system_user_mutations_service.send_user_invite(
        db,
        email=email,
        next_login_path="/reseller/auth/login?next=/reseller/dashboard",
    )


def get_reseller_by_id(db: Session, reseller_id: str) -> Reseller | None:
    """Fetch a reseller by ID, returning None if not found."""
    return db.get(Reseller, coerce_uuid(reseller_id))
