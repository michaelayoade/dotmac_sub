"""Out-of-band notice to a principal whose MFA factors were removed.

Runs as an event consequence after the removal transaction committed, so a
delivery problem can never roll back or block the removal. The message carries
no secrets: only who removed it, when, and what to do if that was unexpected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel
from app.models.subscriber import ResellerUser, Subscriber
from app.models.system_user import SystemUser
from app.schemas.notification import NotificationDeliveryLatency
from app.services.auth_flow import _force_admin_mfa
from app.services.communication_intents import (
    CommunicationClass,
    CommunicationIntent,
)
from app.services.communication_intents import submit as submit_communication_intent
from app.services.events.types import Event, EventType

HANDLED_EVENT_TYPES = frozenset({EventType.credential_administration_completed})
_PRINCIPAL_TYPES = frozenset({"subscriber", "system_user", "reseller_user"})
_FALLBACK_ACTOR = "an administrator"
_SUPPORT_NAME = "Dotmac support"


def _recipient(
    db: Session, principal_type: str, principal_id: UUID
) -> tuple[str | None, str | None]:
    """Return (email on file, display name) for the principal."""

    if principal_type == "system_user":
        user = db.get(SystemUser, principal_id)
        return (user.email, user.display_name) if user else (None, None)
    if principal_type == "reseller_user":
        reseller_user = db.get(ResellerUser, principal_id)
        return (
            (reseller_user.email, reseller_user.full_name)
            if reseller_user
            else (None, None)
        )
    subscriber = db.get(Subscriber, principal_id)
    return (
        (subscriber.email, subscriber.display_name or subscriber.first_name)
        if subscriber
        else (None, None)
    )


def _actor_display(db: Session, actor: str | None, *, recipient_is_staff: bool) -> str:
    """Name shown as the remover. Non-staff recipients never see staff identity."""

    if not recipient_is_staff:
        return _SUPPORT_NAME
    raw = (actor or "").strip()
    if raw.startswith("user:"):
        raw = raw[len("user:") :]
    try:
        user = db.get(SystemUser, UUID(raw))
    except ValueError:
        return _FALLBACK_ACTOR
    name = (user.display_name or "").strip() if user else ""
    return name or _FALLBACK_ACTOR


def render_notice(
    *, actor_name: str, removed_at: datetime, forced_enrolment: bool = False
) -> tuple[str, str]:
    when = removed_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    follow_up = (
        "You will be asked to set up MFA again at your next sign-in."
        if forced_enrolment
        else "Please set up MFA again from your account security settings."
    )
    body = (
        f"Your multi-factor authentication was removed by {actor_name} at "
        f"{when}. If you didn't request this, contact support or security "
        f"immediately. {follow_up}"
    )
    return "Your multi-factor authentication was removed", body


class MfaRemovalNoticeHandler:
    """Expand an MFA-removal event into one deduplicated email intent."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES:
            return
        if not event.payload.get("notify_mfa_removal"):
            return
        if event.payload.get("schema_version") != 1:
            raise ValueError("Unsupported MFA removal event schema")
        try:
            principal_type = str(event.payload["principal_type"])
            principal_id = UUID(str(event.payload["principal_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid MFA removal event context") from exc
        if principal_type not in _PRINCIPAL_TYPES:
            raise ValueError("Unsupported MFA removal principal type")
        email, _name = _recipient(db, principal_type, principal_id)
        if not email or not email.strip():
            return
        raw_at = event.payload.get("removed_at")
        try:
            removed_at = datetime.fromisoformat(str(raw_at))
        except ValueError:
            removed_at = event.occurred_at
        is_staff = principal_type == "system_user"
        subject, body = render_notice(
            actor_name=_actor_display(db, event.actor, recipient_is_staff=is_staff),
            removed_at=removed_at,
            forced_enrolment=is_staff and _force_admin_mfa(db),
        )
        submit_communication_intent(
            db,
            CommunicationIntent(
                subscriber_id=(
                    principal_id if principal_type == "subscriber" else None
                ),
                event_type="auth.mfa_removed",
                category="credentials",
                subject=subject,
                body=body,
                communication_class=CommunicationClass.transactional,
                channels=(NotificationChannel.email,),
                include_reseller=False,
                persist_policy_suppressions=False,
                recipients={NotificationChannel.email: email.strip()},
                audience_type=principal_type,
                audience_id=principal_id,
                resolve_subscriber_identity=False,
                metadata={"source_event_id": str(event.event_id)},
                dedupe_key=f"auth:mfa-removed:{event.event_id}",
                delivery_latency=NotificationDeliveryLatency.immediate,
            ),
        )
