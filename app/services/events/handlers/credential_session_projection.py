"""Idempotent authentication projection repair after credential transitions."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.services import auth_cache
from app.services.events.types import Event, EventType

HANDLED_EVENT_TYPES = frozenset(
    {
        EventType.password_recovery_completed,
        EventType.customer_credential_enrollment_completed,
    }
)


class CredentialSessionProjectionHandler:
    """Project committed credential state into cache-backed authentication stores."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES:
            return
        if event.payload.get("schema_version") != 1:
            raise ValueError("Unsupported credential transition event schema")
        try:
            principal_type = str(event.payload["principal_type"])
            principal_id = UUID(str(event.payload["principal_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid credential transition event context") from exc
        if principal_type not in {"subscriber", "system_user", "reseller_user"}:
            raise ValueError("Unsupported password recovery principal type")

        auth_cache.invalidate_principal_strict(principal_type, str(principal_id))
        if event.event_type is EventType.customer_credential_enrollment_completed:
            if principal_type != "subscriber":
                raise ValueError("Credential enrollment requires a subscriber")
            return
        if principal_type == "subscriber":
            from app.services import customer_portal_session, reseller_portal

            customer_portal_session.revoke_customer_sessions_for_subscriber(
                str(principal_id), db=db, require_durable=True
            )
            reseller_portal.revoke_reseller_sessions_for_subscriber(
                str(principal_id), db=db, require_durable=True
            )
        elif principal_type == "reseller_user":
            from app.services import reseller_portal

            reseller_portal.revoke_reseller_sessions_for_principal(
                principal_id,
                db=db,
                require_durable=True,
            )
