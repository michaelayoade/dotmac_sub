"""Broker for live-chat sessions against the CRM chat_widget channel.

The customer (web/mobile) and reseller portals are already authenticated, so the
sub asserts the caller's identity server-to-server and the CRM mints an
already-identified visitor session. The browser/app never supplies an email, so
there is nothing to spoof: the public ``identify`` endpoint is bypassed entirely.

Customer and reseller chats land in the same general support pool (same
``CRM_CHAT_CONFIG_ID``); the only difference is the ``surface`` tag carried in
session metadata for agent context and reporting — it does not steer routing.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.models.subscriber import Reseller, ResellerUser, Subscriber
from app.services.common import coerce_uuid
from app.services.crm_client import CRMClientError, get_crm_client
from app.services.crm_portal import resolve_crm_subscriber_id

logger = logging.getLogger(__name__)


def _ws_url() -> str:
    """Visitor WebSocket URL, derived from the CRM base URL when not set."""
    if settings.crm_chat_ws_url:
        return settings.crm_chat_ws_url
    base = settings.crm_base_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/ws/widget"


def _api_base() -> str:
    return f"{settings.crm_base_url.rstrip('/')}/widget"


def _require_enabled() -> None:
    if not settings.chat_live_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Live chat is not enabled.",
        )
    if not settings.crm_chat_config_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Live chat is not configured.",
        )


def _mint(
    *,
    email: str,
    name: str | None,
    crm_subscriber_id: str | None,
    metadata: dict,
) -> dict:
    """Call the CRM trusted-mint endpoint and normalise its response."""
    if not email:
        # Identity is asserted by the sub; a principal with no contactable
        # identity can't be linked to a CRM Person.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Account has no email on file for chat.",
        )
    try:
        data = get_crm_client().create_widget_session(
            config_id=settings.crm_chat_config_id,
            email=email,
            name=name,
            crm_subscriber_id=crm_subscriber_id,
            metadata=metadata,
        )
    except CRMClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Chat service is temporarily unavailable.",
        ) from exc

    token = str(data.get("visitor_token") or "")
    session_id = str(data.get("session_id") or "")
    if not token or not session_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Chat service returned an invalid session.",
        )
    conversation_id = data.get("conversation_id")
    return {
        "session_id": session_id,
        "visitor_token": token,
        "conversation_id": str(conversation_id) if conversation_id else None,
        "ws_url": _ws_url(),
        "api_base": _api_base(),
    }


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


def _with_context(
    metadata: dict, *, ticket_id: str | None, project_id: str | None
) -> dict:
    """Attach ticket/project context so the agent sees what the chat is about.

    Carried in session metadata (the CRM merges it onto the chat session), so a
    customer can 'engage us on this ticket/project' and the agent has the
    reference. Ticket wins if both are somehow supplied.
    """
    if ticket_id:
        metadata["ticket_id"] = str(ticket_id)
        metadata["subject"] = "Chat about a support ticket"
    elif project_id:
        metadata["project_id"] = str(project_id)
        metadata["subject"] = "Chat about an installation project"
    return metadata


def broker_customer_session(
    db: Session,
    subscriber_id: str,
    *,
    ticket_id: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Mint a chat session for an authenticated customer, optionally scoped to a
    ticket or project the customer is chatting about."""
    _require_enabled()
    sub = db.get(Subscriber, coerce_uuid(subscriber_id))
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    name = sub.display_name or f"{sub.first_name} {sub.last_name}".strip()
    ticket_id, project_id = _customer_context(
        db, sub.id, ticket_id=ticket_id, project_id=project_id
    )
    metadata = _with_context(
        {"surface": "customer", "subscriber_id": str(sub.id)},
        ticket_id=ticket_id,
        project_id=project_id,
    )
    return _mint(
        email=sub.email or "",
        name=name or None,
        crm_subscriber_id=resolve_crm_subscriber_id(db, str(sub.id)),
        metadata=metadata,
    )


def broker_reseller_session(
    db: Session,
    reseller_id: str,
    principal: dict,
    *,
    ticket_id: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Mint a chat session for an authenticated reseller (general pool),
    optionally scoped to a ticket or project.

    Identity prefers the reseller_user that is logged in (Layer 3); otherwise it
    falls back to the reseller org's contact details.
    """
    _require_enabled()
    reseller = db.get(Reseller, coerce_uuid(reseller_id))
    if reseller is None:
        raise HTTPException(status_code=404, detail="Reseller not found")

    email: str | None = None
    name: str | None = None
    if principal.get("principal_type") == "reseller_user":
        ru = db.get(ResellerUser, coerce_uuid(principal.get("principal_id")))
        if ru is not None:
            email = ru.email
            name = ru.full_name
    email = email or reseller.contact_email
    name = name or reseller.name

    ticket_id, project_id = _reseller_context(
        db, reseller.id, ticket_id=ticket_id, project_id=project_id
    )
    metadata = _with_context(
        {"surface": "reseller_portal", "reseller_id": str(reseller.id)},
        ticket_id=ticket_id,
        project_id=project_id,
    )
    return _mint(
        email=email or "",
        name=name,
        crm_subscriber_id=None,
        metadata=metadata,
    )
