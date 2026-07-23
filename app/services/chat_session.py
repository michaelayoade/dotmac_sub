"""Thin authenticated-chat adapter around the native Team Inbox owner."""

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
