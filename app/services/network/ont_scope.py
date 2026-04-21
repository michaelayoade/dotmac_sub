"""Authorization-scope helpers for ONT write actions."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntAssignment, OntUnit
from app.models.subscriber import Subscriber, UserType


def _uuid_or_none(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _auth_from_request(request: object | None) -> dict | None:
    state = getattr(request, "state", None)
    auth = getattr(state, "auth", None) if state is not None else None
    return auth if isinstance(auth, dict) else None


def can_manage_ont(auth: dict | None, db: Session, ont: OntUnit | None) -> bool:
    """Return whether the authenticated actor can perform writes for an ONT.

    System users are internal operators and may manage ONTs. Subscriber principals
    are tenant-scoped: customer users can manage their own assigned ONTs, and
    reseller users can manage ONTs assigned to subscribers linked to that reseller.
    """
    if ont is None:
        return False
    if auth is None:
        return True
    if "admin" in set(auth.get("roles") or []):
        return True
    if str(auth.get("principal_type") or "") == "system_user":
        return True

    actor_id = _uuid_or_none(auth.get("principal_id") or auth.get("subscriber_id"))
    if actor_id is None:
        return False

    actor = db.get(Subscriber, actor_id)
    if actor is None:
        return False
    if actor.user_type == UserType.system_user:
        return True

    if actor.user_type == UserType.reseller and actor.reseller_id is not None:
        stmt = (
            select(OntAssignment.id)
            .join(Subscriber, OntAssignment.subscriber_id == Subscriber.id)
            .where(OntAssignment.ont_unit_id == ont.id)
            .where(OntAssignment.active.is_(True))
            .where(Subscriber.reseller_id == actor.reseller_id)
            .limit(1)
        )
        return db.scalars(stmt).first() is not None

    stmt = (
        select(OntAssignment.id)
        .where(OntAssignment.ont_unit_id == ont.id)
        .where(OntAssignment.active.is_(True))
        .where(OntAssignment.subscriber_id == actor.id)
        .limit(1)
    )
    return db.scalars(stmt).first() is not None


def is_internal_operator(auth: dict | None, db: Session) -> bool:
    if auth is None:
        return True
    if "admin" in set(auth.get("roles") or []):
        return True
    if str(auth.get("principal_type") or "") == "system_user":
        return True
    actor_id = _uuid_or_none(auth.get("principal_id") or auth.get("subscriber_id"))
    if actor_id is None:
        return False
    actor = db.get(Subscriber, actor_id)
    return bool(actor and actor.user_type == UserType.system_user)


def can_manage_ont_id(auth: dict | None, db: Session, ont_id: object) -> bool:
    ont_uuid = _uuid_or_none(ont_id)
    if ont_uuid is None:
        return False
    return can_manage_ont(auth, db, db.get(OntUnit, ont_uuid))


def can_manage_ont_from_request(
    request: object | None, db: Session, ont_id: object
) -> bool:
    return can_manage_ont_id(_auth_from_request(request), db, ont_id)
