from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.service_team import ServiceTeam
from app.models.team_inbox import InboxConversation, InboxQueueNotification
from app.services import team_inbox_queue_notifications
from app.services.owner_commands import CommandContext
from app.services.team_inbox_assignment import queue_conversation_for_team


def _team(db_session) -> ServiceTeam:
    team = ServiceTeam(name=f"Queue Notice {uuid4()}", team_type="support")
    db_session.add(team)
    db_session.flush()
    return team


def _conversation(db_session) -> InboxConversation:
    conversation = InboxConversation(
        channel_type="whatsapp",
        status="open",
        contact_address="2348012345678",
        external_thread_id=f"queue-{uuid4()}",
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def test_initial_queue_notice_is_recorded(db_session):
    team = _team(db_session)
    conversation = _conversation(db_session)

    queue_conversation_for_team(
        db_session, conversation=conversation, service_team_id=team.id
    )

    notice = db_session.query(InboxQueueNotification).one()
    assert notice.notification_kind == "initial"
    assert notice.queue_position == 1
    assert notice.status in {"sent", "failed"}


def test_queue_notification_sweep_waits_for_position_change_or_heartbeat(db_session):
    team = _team(db_session)
    conversation = _conversation(db_session)
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    queue_conversation_for_team(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        now=now,
    )
    db_session.commit()

    early = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=4),
        ),
    )
    assert early.sent == 0

    heartbeat = team_inbox_queue_notifications.sweep_queue_notifications(
        db_session,
        team_inbox_queue_notifications.QueueNotificationSweepCommand(
            context=CommandContext.system(
                actor="test", scope="team-inbox:routing-command", reason="test"
            ),
            now=now + timedelta(minutes=16),
        ),
    )
    assert heartbeat.sent + heartbeat.failed == 1
