"""Customer-visible FIFO queue notices for Team Inbox."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ai_intake import AiIntakePolicyVersion, AiIntakeSession
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationQueueEntry,
    InboxQueueEntryStatus,
    InboxQueueNotification,
)
from app.services import ai_conversation_intake, team_inbox_outbound
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "communications.team_inbox_routing"
_QUEUE_NOTICE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="Team Inbox FIFO queue customer notifications",
    name="execute_team_inbox_queue_notification_command",
)

SUPPORTED_NOTICE_CHANNELS = frozenset(
    {
        InboxChannelType.whatsapp.value,
        InboxChannelType.facebook_messenger.value,
        InboxChannelType.instagram_dm.value,
    }
)


@dataclass(frozen=True, slots=True)
class QueueNotificationSweepCommand:
    context: CommandContext
    limit: int = 200
    now: datetime | None = None


@dataclass(frozen=True, slots=True)
class QueueNotificationSweepResult:
    sent: int
    skipped: int
    failed: int


def current_queue_position(db: Session, entry: InboxConversationQueueEntry) -> int:
    ahead = (
        db.query(func.count(InboxConversationQueueEntry.id))
        .filter(InboxConversationQueueEntry.service_team_id == entry.service_team_id)
        .filter(
            InboxConversationQueueEntry.status == InboxQueueEntryStatus.queued.value
        )
        .filter(
            (InboxConversationQueueEntry.entered_at < entry.entered_at)
            | (
                (InboxConversationQueueEntry.entered_at == entry.entered_at)
                & (InboxConversationQueueEntry.queue_position <= entry.queue_position)
            )
        )
        .scalar()
        or 1
    )
    return int(ahead)


def _last_sent_notice(
    db: Session, entry_id: UUID, kinds: tuple[str, ...]
) -> InboxQueueNotification | None:
    return (
        db.query(InboxQueueNotification)
        .filter(InboxQueueNotification.queue_entry_id == entry_id)
        .filter(InboxQueueNotification.notification_kind.in_(kinds))
        .filter(InboxQueueNotification.status == "sent")
        .order_by(InboxQueueNotification.sent_at.desc())
        .first()
    )


def _queue_policy(db: Session, conversation: InboxConversation) -> dict[str, object]:
    session = None
    session_id = dict(conversation.metadata_ or {}).get("ai_intake_session_id")
    if session_id:
        try:
            session = db.get(AiIntakeSession, UUID(str(session_id)))
        except (TypeError, ValueError):
            session = None
    version = (
        db.get(AiIntakePolicyVersion, session.policy_version_id)
        if session is not None and session.policy_version_id is not None
        else None
    )
    raw = dict(version.queue_templates or {}) if version is not None else {}
    return {
        **ai_conversation_intake.DEFAULT_QUEUE_TEMPLATES,
        **{
            key: str(raw[key])
            for key in ai_conversation_intake.DEFAULT_QUEUE_TEMPLATES
            if str(raw.get(key) or "").strip()
        },
        "position_update_minutes": int(
            raw.get("position_update_minutes")
            or ai_conversation_intake.DEFAULT_QUEUE_POSITION_UPDATE_MINUTES
        ),
        "heartbeat_minutes": int(
            raw.get("heartbeat_minutes")
            or ai_conversation_intake.DEFAULT_QUEUE_HEARTBEAT_MINUTES
        ),
        "display_name": (
            session.display_name
            if session is not None
            else ai_conversation_intake.DEFAULT_DISPLAY_NAME
        ),
    }


def _send_notice(
    db: Session,
    *,
    entry: InboxConversationQueueEntry,
    conversation: InboxConversation,
    kind: str,
    position: int,
    body: str,
    now: datetime,
) -> InboxQueueNotification:
    dedupe_key = (
        f"queue-notice:{entry.id}:{kind}:{position}:{now.strftime('%Y%m%d%H%M')}"
    )
    existing = (
        db.query(InboxQueueNotification)
        .filter(InboxQueueNotification.dedupe_key == dedupe_key)
        .one_or_none()
    )
    if existing is not None:
        return existing
    notice = InboxQueueNotification(
        queue_entry_id=entry.id,
        conversation_id=conversation.id,
        notification_kind=kind,
        queue_position=position,
        status="pending",
        dedupe_key=dedupe_key,
        next_due_at=now
        + timedelta(
            minutes=int(_queue_policy(db, conversation)["position_update_minutes"])
        ),
        metadata_={"source": "team_inbox_queue_notifications"},
    )
    db.add(notice)
    db.flush()
    policy = _queue_policy(db, conversation)
    display_name = str(policy["display_name"])
    result = team_inbox_outbound.send_ai_intake_message(
        db,
        conversation=conversation,
        body_text=body,
        metadata={
            "sender_type": "ai",
            "author_type": "ai",
            "automation_kind": "queue_notification",
            "ai_display_name": display_name,
            "author_name": display_name,
            "ai_message_purpose": f"queue_{kind}",
            "queue_entry_id": str(entry.id),
            "queue_position": position,
        },
        dedupe_key=dedupe_key,
        now=now,
    )
    notice.status = "sent" if result.kind == "queued" else "failed"
    notice.outbound_message_id = UUID(result.message_id) if result.message_id else None
    notice.sent_at = now if result.kind == "queued" else None
    notice.metadata_ = {
        **dict(notice.metadata_ or {}),
        "delivery_kind": result.kind,
        "delivery_reason": result.reason,
    }
    db.flush()
    return notice


def send_initial_queue_notice(
    db: Session,
    *,
    entry: InboxConversationQueueEntry,
    conversation: InboxConversation,
    now: datetime | None = None,
) -> InboxQueueNotification | None:
    observed_at = now or datetime.now(UTC)
    if (
        entry.status != InboxQueueEntryStatus.queued.value
        or conversation.channel_type not in SUPPORTED_NOTICE_CHANNELS
    ):
        return None
    position = current_queue_position(db, entry)
    policy = _queue_policy(db, conversation)
    return _send_notice(
        db,
        entry=entry,
        conversation=conversation,
        kind="initial",
        position=position,
        body=str(policy["initial"]).format(position=position),
        now=observed_at,
    )


def send_handoff_notice(
    db: Session,
    *,
    conversation: InboxConversation,
    entry: InboxConversationQueueEntry | None,
    now: datetime | None = None,
) -> InboxQueueNotification | None:
    if entry is None:
        return None
    if conversation.channel_type not in SUPPORTED_NOTICE_CHANNELS:
        return None
    observed_at = now or datetime.now(UTC)
    policy = _queue_policy(db, conversation)
    return _send_notice(
        db,
        entry=entry,
        conversation=conversation,
        kind="handoff",
        position=0,
        body=str(policy["handoff"]),
        now=observed_at,
    )


def sweep_queue_notifications(
    db: Session, command: QueueNotificationSweepCommand
) -> QueueNotificationSweepResult:
    observed_at = command.now or datetime.now(UTC)

    def _operation() -> QueueNotificationSweepResult:
        sent = 0
        skipped = 0
        failed = 0
        entries = (
            db.query(InboxConversationQueueEntry)
            .filter(
                InboxConversationQueueEntry.status == InboxQueueEntryStatus.queued.value
            )
            .order_by(InboxConversationQueueEntry.entered_at.asc())
            .limit(command.limit)
            .with_for_update(skip_locked=True)
            .all()
        )
        for entry in entries:
            conversation = db.get(InboxConversation, entry.conversation_id)
            if conversation is None or not conversation.is_active:
                skipped += 1
                continue
            position = current_queue_position(db, entry)
            last_any = _last_sent_notice(
                db, entry.id, ("initial", "position_update", "heartbeat")
            )
            policy = _queue_policy(db, conversation)
            update_minutes = int(policy["position_update_minutes"])
            heartbeat_minutes = int(policy["heartbeat_minutes"])
            if last_any is None:
                notice = send_initial_queue_notice(
                    db, entry=entry, conversation=conversation, now=observed_at
                )
            else:
                elapsed = (
                    observed_at - last_any.sent_at
                    if last_any.sent_at is not None
                    else timedelta()
                )
                if (
                    last_any.queue_position is not None
                    and position != last_any.queue_position
                    and elapsed >= timedelta(minutes=update_minutes)
                ):
                    notice = _send_notice(
                        db,
                        entry=entry,
                        conversation=conversation,
                        kind="position_update",
                        position=position,
                        body=str(policy["position_update"]).format(position=position),
                        now=observed_at,
                    )
                elif elapsed >= timedelta(minutes=heartbeat_minutes):
                    notice = _send_notice(
                        db,
                        entry=entry,
                        conversation=conversation,
                        kind="heartbeat",
                        position=position,
                        body=str(policy["heartbeat"]).format(position=position),
                        now=observed_at,
                    )
                else:
                    notice = None
            if notice is None:
                skipped += 1
            elif notice.status == "sent":
                sent += 1
            else:
                failed += 1
        return QueueNotificationSweepResult(sent=sent, skipped=skipped, failed=failed)

    return execute_owner_command(
        db,
        definition=_QUEUE_NOTICE_COMMAND,
        context=command.context,
        operation=_operation,
    )
