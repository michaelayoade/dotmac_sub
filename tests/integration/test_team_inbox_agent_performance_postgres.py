from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import team_inbox_metrics
from tests.staff_identity_fixtures import add_bound_staff_user


def test_agent_performance_analytics_runs_on_postgres_without_sla_thresholds(
    db_session,
) -> None:
    assert db_session.bind.dialect.name == "postgresql"

    team = ServiceTeam(
        name=f"Support {uuid4().hex}",
        team_type=ServiceTeamType.support.value,
        metadata_={},
    )
    db_session.add(team)
    db_session.flush()

    user, person = add_bound_staff_user(db_session)
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )

    base = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    conversation = InboxConversation(
        channel_type="email",
        status="open",
        subject="Need help",
        contact_address="customer@example.com",
        primary_service_team_id=team.id,
        first_message_at=base,
        last_message_at=base + timedelta(minutes=5),
    )
    db_session.add(conversation)
    db_session.flush()

    db_session.add_all(
        [
            InboxMessage(
                conversation_id=conversation.id,
                channel_type="email",
                direction=InboxMessageDirection.inbound.value,
                subject="Message",
                body="Body",
                received_at=base,
            ),
            InboxMessage(
                conversation_id=conversation.id,
                channel_type="email",
                direction=InboxMessageDirection.outbound.value,
                subject="Message",
                body="Body",
                sent_at=base + timedelta(minutes=5),
                metadata_={"sent_by_person_id": str(user.id)},
            ),
            InboxConversationAssignment(
                conversation_id=conversation.id,
                service_team_id=team.id,
                person_id=user.id,
                assigned_at=base,
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    page = team_inbox_metrics.agent_performance_analytics(
        db_session,
        query=team_inbox_metrics.InboxAgentPerformanceQuery(
            start_at=base - timedelta(minutes=1),
            end_at=base + timedelta(days=1),
            per_page=10,
        ),
    )

    assert page.summary.assigned_conversation_count == 1
    assert page.summary.average_first_response_seconds == 300
    assert page.summary.sla_configured is False
    assert page.rows[0].first_response_sla_seconds is None
    assert page.rows[0].resolution_sla_seconds is None
