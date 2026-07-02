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

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.models.subscriber import Reseller, ResellerUser, Subscriber
from app.services.common import coerce_uuid
from app.services.crm_client import CRMClientError, get_crm_client
from app.services.crm_portal import resolve_crm_subscriber_id


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
