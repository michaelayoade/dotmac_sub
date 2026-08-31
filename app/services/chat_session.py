"""Portal live-chat broker seam: one authority, one destination.

Sub's native Team Inbox is the ONLY live-chat authority. This module exists
solely to release the adapter's read transaction before the owning command
enters `execute_owner_command` on a transaction-free session; it makes no
routing decision and has no second destination to route to.

That is deliberate and load bearing. From 2026-07-27 until 2026-08-30 a
`comms.chat_session_authority` setting COULD have selected an external CRM
transport here instead (ADR 0006) -- whether production ever did is not
knowable from this repository, and the CRM was deleted on 2026-08-29 without a
final backup, so any conversation that lived only there is gone. The selector
is removed, not re-pointed: a live-chat surface with two possible writers
loses operator visibility the moment the two disagree, and reconciling them
afterwards is unbounded work. Do not reintroduce a selector, a second broker
destination, or a fallback that writes locally when a remote transport is
unavailable.

Enforced by `tests/architecture/test_single_chat_authority.py`.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db import finish_read_transaction
from app.services import team_inbox_widget


def broker_customer_session(
    db: Session,
    subscriber_id: str,
    *,
    ticket_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, str | None]:
    finish_read_transaction(db)
    return team_inbox_widget.broker_customer_session_committed(
        db,
        subscriber_id,
        ticket_id=ticket_id,
        project_id=project_id,
    )


def broker_reseller_session(
    db: Session,
    reseller_id: str,
    principal: dict[str, object],
    *,
    ticket_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, str | None]:
    finish_read_transaction(db)
    return team_inbox_widget.broker_reseller_session_committed(
        db,
        reseller_id,
        principal,
        ticket_id=ticket_id,
        project_id=project_id,
    )
