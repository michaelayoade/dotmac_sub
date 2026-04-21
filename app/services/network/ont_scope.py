"""Authorization-scope helpers for ONT write actions."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntAssignment, OntUnit
from app.models.subscriber import Subscriber, UserType

logger = logging.getLogger(__name__)


def _uuid_or_none(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _auth_from_request(request: object | None) -> dict | None:
    state = getattr(request, "state", None)
    auth = getattr(state, "auth", None) if state is not None else None
    return auth if isinstance(auth, dict) else None


def _request_path(request: object | None) -> str:
    return str(getattr(getattr(request, "url", None), "path", "") or "")


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
    request: object | None,
    db: Session,
    ont_id: object,
    *,
    allow_missing_auth: bool = False,
) -> bool:
    auth = _auth_from_request(request)
    if auth is None and not allow_missing_auth:
        logger.warning(
            "Denied ONT scope check due to missing request auth: path=%s ont_id=%s",
            _request_path(request),
            ont_id,
        )
        return False
    return can_manage_ont_id(auth, db, ont_id)


def is_internal_operator_from_request(
    request: object | None,
    db: Session,
    *,
    allow_missing_auth: bool = False,
) -> bool:
    auth = _auth_from_request(request)
    if auth is None and not allow_missing_auth:
        logger.warning(
            "Denied internal-operator check due to missing request auth: path=%s",
            _request_path(request),
        )
        return False
    return is_internal_operator(auth, db)


def can_authorize_ont_from_request(
    request: object | None,
    db: Session,
    ont_id: object,
    *,
    allow_missing_auth: bool = False,
) -> bool:
    ont_id_text = str(ont_id or "").strip()
    if ont_id_text:
        return can_manage_ont_from_request(
            request,
            db,
            ont_id_text,
            allow_missing_auth=allow_missing_auth,
        )
    return is_internal_operator_from_request(
        request,
        db,
        allow_missing_auth=allow_missing_auth,
    )


def filter_manageable_ont_ids_from_request(
    request: object | None,
    db: Session,
    ont_ids: list[str],
    *,
    allow_missing_auth: bool = False,
) -> list[str]:
    auth = _auth_from_request(request)
    if auth is None and not allow_missing_auth:
        logger.warning(
            "Denied bulk ONT scope check due to missing request auth: path=%s count=%s",
            _request_path(request),
            len(ont_ids),
        )
        return []
    return [ont_id for ont_id in ont_ids if can_manage_ont_id(auth, db, ont_id)]
