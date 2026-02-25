from __future__ import annotations

from app.models.audit import AuditActorType, AuditEvent
from app.services.web_subscriber_details import build_subscriber_timeline


def test_timeline_includes_todo_flags_for_comment_events(db_session, subscriber):
    event = AuditEvent(
        actor_type=AuditActorType.user,
        actor_id=str(subscriber.id),
        action="comment",
        entity_type="subscriber",
        entity_id=str(subscriber.id),
        is_success=True,
        metadata_={
            "comment": "Follow up tomorrow",
            "is_todo": True,
            "is_completed": False,
        },
    )
    db_session.add(event)
    db_session.commit()

    timeline = build_subscriber_timeline(db_session, subscriber.id)

    assert len(timeline) >= 1
    first = timeline[0]
    assert first["is_todo"] is True
    assert first["is_completed"] is False
    assert "Follow up tomorrow" in first["detail"]
