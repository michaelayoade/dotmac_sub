"""Push customer-created tickets and comments from Sub to DotMac Omni CRM.

Customer tickets are created locally (portal web + mobile API) and were
previously invisible to agents working in the CRM. On creation we push the
ticket to the CRM and link it via metadata crm_ticket_id; from then on the
inbound pull maintains it like any other CRM ticket (sync_source=crm), and
local customer replies are pushed as CRM comments.

Echo-guards: comments that already carry a crm_comment_id (i.e. they were
imported by the pull, or already pushed) are never pushed again — and pushed
comments get their CRM id stored so the pull never re-imports them.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.support import Ticket, TicketComment
from app.services.crm_client import get_crm_client

logger = logging.getLogger(__name__)


class TicketNotLinkedError(Exception):
    """Comment push attempted before its ticket has a CRM counterpart."""


def _coerce(value: str) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def push_ticket(db: Session, ticket_id: str) -> str:
    """Create the CRM counterpart of a local ticket. Returns an outcome key."""
    parsed = _coerce(ticket_id)
    ticket = db.get(Ticket, parsed) if parsed else None
    if ticket is None:
        return "missing"
    metadata = dict(ticket.metadata_ or {})
    if metadata.get("crm_ticket_id"):
        return "already_linked"
    if not ticket.subscriber_id:
        return "no_subscriber"

    from app.services.crm_portal import resolve_crm_subscriber_id

    crm_subscriber_id = resolve_crm_subscriber_id(db, str(ticket.subscriber_id))
    if not crm_subscriber_id:
        return "unresolved_subscriber"

    client = get_crm_client()
    payload: dict[str, Any] = {
        "title": ticket.title,
        "description": ticket.description or "",
        "priority": ticket.priority or "normal",
        "subscriber_id": crm_subscriber_id,
        "metadata_": {"origin": "dotmac_sub", "sub_ticket_id": str(ticket.id)},
    }
    created = client.create_ticket(payload)
    crm_ticket_id = str(created.get("id") or "")
    if not crm_ticket_id:
        logger.warning("CRM ticket create returned no id for %s", ticket.id)
        return "no_crm_id"

    metadata.update(
        {
            "sync_source": "crm",
            "crm_ticket_id": crm_ticket_id,
            "crm_ticket_number": str(created.get("number") or "") or None,
            "crm_created_at": created.get("created_at"),
            "crm_updated_at": created.get("updated_at"),
        }
    )
    ticket.metadata_ = {k: v for k, v in metadata.items() if v is not None}
    # The CRM is the numbering authority for synced tickets (the pull would
    # adopt its number on the next update anyway).
    crm_number = str(created.get("number") or "").strip()
    if crm_number:
        ticket.number = crm_number
    db.commit()
    return "pushed"


def push_comment(db: Session, comment_id: str) -> str:
    """Create the CRM counterpart of a local customer comment."""
    parsed = _coerce(comment_id)
    comment = db.get(TicketComment, parsed) if parsed else None
    if comment is None:
        return "missing"
    comment_metadata = dict(comment.metadata_ or {})
    if comment_metadata.get("crm_comment_id"):
        # Imported by the pull or already pushed — never echo back.
        return "already_linked"
    if comment.is_internal:
        return "internal_skipped"

    ticket = db.get(Ticket, comment.ticket_id)
    crm_ticket_id = str(
        ((ticket.metadata_ if ticket else None) or {}).get("crm_ticket_id") or ""
    )
    if not crm_ticket_id:
        # Ticket push may still be in flight — retried by the task.
        raise TicketNotLinkedError(f"ticket {comment.ticket_id} has no CRM link")

    client = get_crm_client()
    created = client.create_ticket_comment(
        {
            "ticket_id": crm_ticket_id,
            "body": comment.body,
            "is_internal": False,
        }
    )
    crm_comment_id = str(created.get("id") or "")
    if crm_comment_id:
        comment_metadata.update(
            {"crm_comment_id": crm_comment_id, "sync_source": "sub"}
        )
        comment.metadata_ = comment_metadata
        db.commit()
    return "pushed"


def enqueue_crm_ticket_push(ticket_id: str | UUID, *, source: str) -> None:
    """Queue a ticket push; never raises into the customer-facing flow."""
    from app.config import settings

    if not settings.crm_base_url:
        return
    try:
        from app.services.queue_adapter import enqueue_task
        from app.tasks.crm_ticket_push import push_ticket_to_crm

        enqueue_task(push_ticket_to_crm, args=[str(ticket_id)], source=source)
    except Exception as exc:  # noqa: BLE001
        logger.warning("CRM ticket push enqueue failed for %s: %s", ticket_id, exc)


def enqueue_crm_comment_push(comment_id: str | UUID, *, source: str) -> None:
    """Queue a comment push; never raises into the customer-facing flow."""
    from app.config import settings

    if not settings.crm_base_url:
        return
    try:
        from app.services.queue_adapter import enqueue_task
        from app.tasks.crm_ticket_push import push_comment_to_crm

        enqueue_task(push_comment_to_crm, args=[str(comment_id)], source=source)
    except Exception as exc:  # noqa: BLE001
        logger.warning("CRM comment push enqueue failed for %s: %s", comment_id, exc)
