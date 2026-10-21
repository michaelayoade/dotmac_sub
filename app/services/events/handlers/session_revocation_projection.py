"""Retryable cleanup of exact committed session revocation targets."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.services import auth_cache
from app.services.events.types import Event, EventType

HANDLED_EVENT_TYPES = frozenset({EventType.authentication_sessions_revoked})


class SessionRevocationProjectionHandler:
    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES:
            return
        if event.payload.get("schema_version") != 1:
            raise ValueError("Unsupported session revocation schema")
        from app.services.auth_session_refresh import SessionPrincipalType
        from app.services.session_manager import _binding

        kind = SessionPrincipalType(event.payload["principal_type"])
        principal_id = UUID(str(event.payload["principal_id"]))
        targets = event.payload.get("session_ids")
        if not isinstance(targets, list):
            raise ValueError("Invalid exact session targets")
        ids = tuple(UUID(str(value)) for value in targets)
        for session_id in ids:
            row = db.scalars(
                select(AuthSession)
                .where(AuthSession.id == session_id)
                .execution_options(populate_existing=True)
            ).first()
            if (
                row is None
                or _binding(row) != (kind, principal_id)
                or row.status != SessionStatus.revoked
                or row.revoked_at is None
            ):
                raise ValueError("Revocation projection has no authoritative standing")
            auth_cache.invalidate_session_context_strict(
                str(session_id),
                principal_type=kind.value,
                principal_id=str(principal_id),
            )
