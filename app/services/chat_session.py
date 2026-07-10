"""Live-chat session broker backed by the native team inbox."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.subscriber import Reseller, Subscriber
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


def _owned_ticket(db: Session, subscriber_id, ticket_id: str) -> bool:
    from app.models.support import Ticket

    tid = coerce_uuid(str(ticket_id))
    if tid is None:
        return False
    return (
        db.query(Ticket.id)
        .filter(
            Ticket.id == tid,
            Ticket.subscriber_id == coerce_uuid(str(subscriber_id)),
        )
        .first()
        is not None
    )


def _owned_project(db: Session, subscriber_id, project_id: str) -> bool:
    from app.models.project_mirror import ProjectMirror

    return (
        db.query(ProjectMirror.crm_project_id)
        .filter(
            ProjectMirror.crm_project_id == str(project_id),
            ProjectMirror.subscriber_id == coerce_uuid(str(subscriber_id)),
        )
        .first()
        is not None
    )


def _reseller_owns(db: Session, reseller_id, subscriber_id) -> bool:
    """True if the given subscriber is one of the reseller's managed accounts."""
    from app.services import reseller_portal

    if subscriber_id is None:
        return False
    return (
        reseller_portal.owned_account(db, str(reseller_id), str(subscriber_id))
        is not None
    )


def _customer_context(db, subscriber_id, *, ticket_id, project_id):
    """Drop any ticket/project the subscriber doesn't own, so a chat cannot be
    scoped to another customer's record (the reference is surfaced to the agent).
    Dropping (vs rejecting) keeps the chat usable even if a legit id can't be
    resolved — the only effect is the agent loses the (unverified) context."""
    if ticket_id and not _owned_ticket(db, subscriber_id, ticket_id):
        logger.warning(
            "chat_ctx_ticket_not_owned sub=%s ticket=%s", subscriber_id, ticket_id
        )
        ticket_id = None
    if project_id and not _owned_project(db, subscriber_id, project_id):
        logger.warning(
            "chat_ctx_project_not_owned sub=%s project=%s", subscriber_id, project_id
        )
        project_id = None
    return ticket_id, project_id


def _reseller_context(db, reseller_id, *, ticket_id, project_id):
    """Drop any ticket/project not belonging to one of the reseller's accounts."""
    from app.models.project_mirror import ProjectMirror
    from app.models.support import Ticket

    if ticket_id:
        tid = coerce_uuid(str(ticket_id))
        owner = (
            db.query(Ticket.subscriber_id).filter(Ticket.id == tid).scalar()
            if tid is not None
            else None
        )
        if not _reseller_owns(db, reseller_id, owner):
            logger.warning(
                "chat_ctx_ticket_not_owned reseller=%s ticket=%s",
                reseller_id,
                ticket_id,
            )
            ticket_id = None
    if project_id:
        owner = (
            db.query(ProjectMirror.subscriber_id)
            .filter(ProjectMirror.crm_project_id == str(project_id))
            .scalar()
        )
        if not _reseller_owns(db, reseller_id, owner):
            logger.warning(
                "chat_ctx_project_not_owned reseller=%s project=%s",
                reseller_id,
                project_id,
            )
            project_id = None
    return ticket_id, project_id


def broker_customer_session(
    db: Session,
    subscriber_id: str,
    *,
    ticket_id: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Mint a native team-inbox chat session for an authenticated customer."""
    from app.services import team_inbox_widget

    sub = db.get(Subscriber, coerce_uuid(subscriber_id))
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    ticket_id, project_id = _customer_context(
        db, sub.id, ticket_id=ticket_id, project_id=project_id
    )
    result = team_inbox_widget.broker_customer_session(
        db,
        subscriber_id,
        ticket_id=ticket_id,
        project_id=project_id,
    )
    db.commit()
    return result


def broker_reseller_session(
    db: Session,
    reseller_id: str,
    principal: dict,
    *,
    ticket_id: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Mint a native team-inbox chat session for an authenticated reseller."""
    from app.services import team_inbox_widget

    reseller = db.get(Reseller, coerce_uuid(reseller_id))
    if reseller is None:
        raise HTTPException(status_code=404, detail="Reseller not found")
    ticket_id, project_id = _reseller_context(
        db, reseller.id, ticket_id=ticket_id, project_id=project_id
    )
    result = team_inbox_widget.broker_reseller_session(
        db,
        reseller_id,
        principal,
        ticket_id=ticket_id,
        project_id=project_id,
    )
    db.commit()
    return result
