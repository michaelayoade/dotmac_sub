"""Just-in-time materialization for communication actions carrying capabilities.

Communication intents and notifications persist only an allowlisted action name
and non-secret canonical context.  The delivery worker calls this owner after
all normal policy gates and immediately before transport.  Rendered content is
returned in memory and must never be written back to the outbox row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.notification import Notification, NotificationChannel

EPHEMERAL_ACTION_METADATA_KEY = "ephemeral_action"
REFERRAL_CREDENTIAL_ENROLLMENT_ACTION = "auth.referral_credential_enrollment"
_ENVELOPE_KEYS = frozenset({"type", "version", "context"})


class EphemeralActionRejected(ValueError):
    """A safe, terminal materialization refusal.

    ``code`` is deliberately low-cardinality and safe to persist in delivery
    state.  Domain exception messages and rendered content are not propagated.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EphemeralEmailContent:
    subject: str
    body_html: str
    body_text: str
    activity: str
    sender_key: str | None = None


def descriptor(
    *, action_type: str, version: int, context: dict[str, object]
) -> dict[str, object]:
    """Build the only durable envelope accepted by the worker."""

    normalized_type = str(action_type or "").strip()
    if not normalized_type or isinstance(version, bool) or version < 1:
        raise ValueError("A typed, versioned ephemeral action is required")
    return {
        "type": normalized_type,
        "version": version,
        "context": dict(context),
    }


def has_ephemeral_action(notification: Notification) -> bool:
    return EPHEMERAL_ACTION_METADATA_KEY in dict(notification.metadata_ or {})


def _envelope(notification: Notification) -> tuple[str, int, dict[str, Any]]:
    raw = dict(notification.metadata_ or {}).get(EPHEMERAL_ACTION_METADATA_KEY)
    if not isinstance(raw, dict) or set(raw) != _ENVELOPE_KEYS:
        raise EphemeralActionRejected("invalid_envelope")
    action_type = raw.get("type")
    version = raw.get("version")
    context = raw.get("context")
    if (
        not isinstance(action_type, str)
        or not action_type.strip()
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
        or not isinstance(context, dict)
    ):
        raise EphemeralActionRejected("invalid_envelope")
    return action_type.strip(), version, context


def materialize_email(db: Session, notification: Notification) -> EphemeralEmailContent:
    """Materialize one allowlisted email action without mutating the outbox."""

    if notification.channel != NotificationChannel.email:
        raise EphemeralActionRejected("unsupported_channel")
    action_type, version, context = _envelope(notification)
    if action_type == REFERRAL_CREDENTIAL_ENROLLMENT_ACTION and version == 1:
        # Lazy import keeps the communications owner independent of the auth
        # domain while retaining an explicit allowlist instead of dynamic code.
        from app.services import customer_credential_enrollment

        content = customer_credential_enrollment.materialize_enrollment_email(
            db,
            notification=notification,
            context=context,
        )
    else:
        raise EphemeralActionRejected("unsupported_action")

    if (
        not isinstance(content, EphemeralEmailContent)
        or not content.subject.strip()
        or len(content.subject) > 200
        or not content.body_html.strip()
        or not content.body_text.strip()
        or not content.activity.strip()
    ):
        raise EphemeralActionRejected("invalid_materialization")
    return content
