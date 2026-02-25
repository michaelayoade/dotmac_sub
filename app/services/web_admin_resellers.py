"""Service helpers for admin reseller management routes."""

from __future__ import annotations

import logging
from typing import cast
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.models.auth import AuthProvider
from app.models.rbac import Role
from app.models.subscriber import Reseller, ResellerUser, Subscriber, UserType
from app.schemas.auth import UserCredentialCreate
from app.schemas.rbac import SubscriberRoleCreate
from app.schemas.subscriber import SubscriberCreate
from app.services import auth as auth_service
from app.services import rbac as rbac_service
from app.services import subscriber as subscriber_service
from app.services.auth_flow import hash_password
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


def create_subscriber_credential(
    db: Session,
    *,
    first_name: str,
    last_name: str,
    email: str,
    username: str,
    password: str,
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
        username=username,
        password_hash=hash_password(password),
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
    try:
        link = ResellerUser(
            reseller_id=reseller_id,
            subscriber_id=subscriber_id,
            is_active=True,
        )
        db.add(link)
        db.flush()
        return link
    except ProgrammingError:
        # Compatibility path for schemas without reseller_users* table.
        db.rollback()
        subscriber = db.get(Subscriber, subscriber_id)
        if not subscriber:
            raise
        subscriber.reseller_id = reseller_id
        subscriber.user_type = getattr(type(subscriber.user_type), "reseller", subscriber.user_type)
        db.flush()
        return cast(ResellerUser, SimpleNamespace(
            id=subscriber.id,
            reseller_id=reseller_id,
            subscriber_id=subscriber.id,
            person_id=subscriber.id,
            is_active=True,
            created_at=subscriber.created_at,
        ))


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
        username=user_payload["username"] or "",
        password=user_payload["password"] or "",
    )
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


def list_reseller_users(
    db: Session,
    reseller_id: str,
) -> list[ResellerUser]:
    """List ResellerUser links for a reseller with subscriber data attached.

    Attaches the related Subscriber as ``link.person`` for template access.
    """
    try:
        stmt = select(ResellerUser).where(
            ResellerUser.reseller_id == coerce_uuid(reseller_id)
        )
        links = list(db.scalars(stmt).all())
        sub_ids = [link.subscriber_id for link in links if link.subscriber_id]
        if sub_ids:
            sub_stmt = select(Subscriber).where(Subscriber.id.in_(sub_ids))
            subscribers_by_id = {s.id: s for s in db.scalars(sub_stmt).all()}
            for link in links:
                if link.subscriber_id is None:
                    continue
                link.person = subscribers_by_id.get(link.subscriber_id)  # type: ignore[attr-defined]
        return links
    except ProgrammingError:
        db.rollback()

    subscribers = list(
        db.scalars(
            select(Subscriber)
            .where(Subscriber.reseller_id == coerce_uuid(reseller_id))
            .where(Subscriber.user_type == UserType.reseller)
            .order_by(Subscriber.created_at.desc())
        ).all()
    )
    links: list[ResellerUser] = []
    for subscriber in subscribers:
        link = cast(ResellerUser, SimpleNamespace(
            id=subscriber.id,
            reseller_id=subscriber.reseller_id,
            subscriber_id=subscriber.id,
            person_id=subscriber.id,
            is_active=subscriber.is_active,
            created_at=subscriber.created_at,
            person=subscriber,
        ))
        links.append(link)
    return links


def list_reseller_people(db: Session) -> list[Subscriber]:
    """List person-type subscribers for linking to resellers."""
    return cast(
        list[Subscriber],
        subscriber_service.subscribers.list(
            db=db,
            organization_id=None,
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
) -> dict | None:
    """Load reseller detail page data: reseller with users and people list.

    Returns None if the reseller is not found.
    """
    reseller = get_reseller_by_id(db, reseller_id)
    if not reseller:
        return None
    reseller_users = list_reseller_users(db, reseller_id)
    people = list_reseller_people(db)
    return {
        "reseller": reseller,
        "reseller_users": reseller_users,
        "people": people,
    }


def link_existing_subscriber_to_reseller(
    db: Session,
    *,
    reseller_id: str,
    subscriber_id: str,
) -> bool:
    """Link an existing subscriber to a reseller. Returns False if already linked."""
    r_uuid = coerce_uuid(reseller_id)
    s_uuid = coerce_uuid(subscriber_id)
    try:
        stmt = (
            select(ResellerUser)
            .where(ResellerUser.reseller_id == r_uuid)
            .where(ResellerUser.subscriber_id == s_uuid)
        )
        existing = db.scalars(stmt).first()
        if existing:
            return False
        create_reseller_user_link(db, reseller_id=r_uuid, subscriber_id=s_uuid)
        db.commit()
        return True
    except ProgrammingError:
        db.rollback()

    subscriber = db.get(Subscriber, s_uuid)
    if not subscriber:
        return False
    if subscriber.reseller_id == r_uuid and str(getattr(subscriber.user_type, "value", subscriber.user_type)) == "reseller":
        return False
    subscriber.reseller_id = r_uuid
    subscriber.user_type = getattr(type(subscriber.user_type), "reseller", subscriber.user_type)
    db.commit()
    return True


def create_and_link_reseller_user(
    db: Session,
    *,
    reseller_id: str,
    first_name: str,
    last_name: str,
    email: str,
    username: str,
    password: str,
) -> None:
    """Create a new subscriber with credentials and link to a reseller."""
    subscriber = create_subscriber_credential(
        db,
        first_name=first_name,
        last_name=last_name,
        email=email,
        username=username,
        password=password,
    )
    create_reseller_user_link(
        db,
        reseller_id=coerce_uuid(reseller_id),
        subscriber_id=subscriber.id,
    )
    db.commit()


def get_reseller_by_id(db: Session, reseller_id: str) -> Reseller | None:
    """Fetch a reseller by ID, returning None if not found."""
    return db.get(Reseller, coerce_uuid(reseller_id))
