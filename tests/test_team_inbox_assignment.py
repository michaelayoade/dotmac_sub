from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.admin_alert import AdminNotification
from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.models.domain_settings import SettingDomain
from app.models.notification import Notification, NotificationChannel
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceEvent,
    InboxAgentPresenceSessionLease,
    InboxAgentPresenceStatus,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationTeam,
    InboxRoutingDecisionMode,
    InboxRoutingEvent,
    InboxRoutingEventType,
    InboxTeamRole,
)
from app.services import (
    settings_spec,
    team_inbox_agent_introduction,
    team_inbox_assignment,
    team_inbox_commands,
    web_system_settings_forms,
)
from app.services.owner_commands import CommandContext
from tests.staff_identity_fixtures import add_bound_staff_user


def _team(db_session, name: str = "Support") -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def _member(
    db_session,
    team: ServiceTeam,
    *,
    status: str = InboxAgentPresenceStatus.online.value,
    max_concurrent: int | None = None,
):
    user, person = add_bound_staff_user(db_session)
    person_id = user.id
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )
    db_session.add(
        InboxAgentPresence(
            person_id=person_id,
            status=status,
            last_seen_at=datetime.now(UTC) if status == "online" else None,
            max_concurrent_conversations=max_concurrent,
        )
    )
    if status == InboxAgentPresenceStatus.online.value:
        auth_session = _staff_session(db_session, user)
        db_session.add(
            InboxAgentPresenceSessionLease(
                person_id=user.id,
                auth_session_id=auth_session.id,
                last_seen_at=datetime.now(UTC),
            )
        )
    db_session.flush()
    return person_id


def _staff_session(db_session, system_user, *, now: datetime | None = None):
    observed_at = now or datetime.now(UTC)
    session = AuthSession(
        system_user_id=system_user.id,
        party_id=system_user.person_party_id,
        status=SessionStatus.active,
        token_hash=f"presence-test:{uuid4()}",
        created_at=observed_at,
        last_seen_at=observed_at,
        expires_at=observed_at + timedelta(days=1),
    )
    db_session.add(session)
    db_session.flush()
    return session


def _conversation(db_session) -> InboxConversation:
    conversation = InboxConversation(channel_type="email", subject="Need help")
    db_session.add(conversation)
    db_session.flush()
    return conversation


def test_available_team_agents_ignore_offline_members(db_session):
    team = _team(db_session)
    online = _member(db_session, team, status=InboxAgentPresenceStatus.online.value)
    _member(db_session, team, status=InboxAgentPresenceStatus.offline.value)
    db_session.commit()

    candidates = team_inbox_assignment.list_available_team_agents(db_session, team.id)

    assert [candidate.person_id for candidate in candidates] == [str(online)]


def test_available_team_agents_respect_manual_presence_override(db_session):
    team = _team(db_session)
    agent = _member(db_session, team, status=InboxAgentPresenceStatus.online.value)
    presence = (
        db_session.query(InboxAgentPresence)
        .filter(InboxAgentPresence.person_id == agent)
        .one()
    )
    presence.manual_override_status = InboxAgentPresenceStatus.away.value
    db_session.commit()

    candidates = team_inbox_assignment.list_available_team_agents(db_session, team.id)

    assert candidates == []


def test_available_team_agents_ignore_full_members(db_session):
    team = _team(db_session)
    full = _member(db_session, team, max_concurrent=1)
    free = _member(db_session, team, max_concurrent=1)
    conversation = _conversation(db_session)
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=full,
            is_active=True,
        )
    )
    db_session.commit()

    candidates = team_inbox_assignment.list_available_team_agents(db_session, team.id)
    availability = team_inbox_assignment.agent_availability_snapshots(
        db_session,
        (full, free),
    )

    assert [candidate.person_id for candidate in candidates] == [str(free)]
    assert availability[full].active_conversation_count == 1
    assert availability[full].max_concurrent_conversations == 1
    assert availability[full].available_capacity == 0
    assert availability[full].assignment_eligible is False
    assert (
        availability[full].unavailability_reason
        is team_inbox_assignment.InboxAgentUnavailabilityReason.at_capacity
    )
    assert availability[free].available_capacity == 1
    assert availability[free].assignment_eligible is True


def test_assign_conversation_escalates_to_team_and_online_agent(db_session):
    team = _team(db_session, "Field Service")
    agent = _member(db_session, team)
    conversation = _conversation(db_session)
    db_session.commit()

    result = team_inbox_assignment.assign_conversation_to_available_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
    )
    db_session.commit()

    link = db_session.query(InboxConversationTeam).one()
    assignment = db_session.query(InboxConversationAssignment).one()
    event = db_session.query(InboxRoutingEvent).one()
    assert result.kind == "assigned"
    assert result.assigned_person_id == str(agent)
    assert conversation.primary_service_team_id == team.id
    assert link.role == InboxTeamRole.owner.value
    assert assignment.person_id == agent
    assert assignment.is_active is True
    assert event.decision_mode is InboxRoutingDecisionMode.automatic
    assert event.presence_status == InboxAgentPresenceStatus.online.value
    assert event.active_conversation_count == 0
    assert event.max_concurrent_conversations == 10


def test_new_assignment_stages_one_personal_notice_and_replay_is_quiet(db_session):
    team = _team(db_session, "Notified Support")
    agent = _member(db_session, team)
    conversation = _conversation(db_session)
    db_session.commit()

    first = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        person_id=agent,
    )
    replay = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        person_id=agent,
    )

    assert first.kind == "assigned"
    assert replay.reason == "already_assigned"
    notifications = db_session.query(Notification).all()
    assert len(notifications) == 2
    assert {row.channel for row in notifications} == {
        NotificationChannel.push,
        NotificationChannel.email,
    }
    assert {row.event_type for row in notifications} == {"team_inbox.assignment"}
    assert all(
        (row.metadata_ or {}).get("source_entity_id") == str(conversation.id)
        for row in notifications
    )
    personal_notice = db_session.query(AdminNotification).one()
    assert personal_notice.system_user_id == agent
    assert personal_notice.target_url == (
        f"/admin/inbox?conversation_id={conversation.id}"
    )


def test_self_assignment_does_not_notify_the_assigning_agent(db_session):
    team = _team(db_session, "Self-assigned Support")
    agent = _member(db_session, team)
    conversation = _conversation(db_session)
    db_session.commit()

    result = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        person_id=agent,
        assigned_by_person_id=agent,
    )

    assert result.kind == "assigned"
    assert db_session.query(Notification).count() == 0
    assert db_session.query(AdminNotification).count() == 0


def test_agent_can_choose_assignment_notification_channels(db_session):
    team = _team(db_session, "Preference Support")
    agent = _member(db_session, team)
    conversation = _conversation(db_session)
    team_inbox_agent_introduction.update_preference(
        db_session,
        person_id=agent,
        template=team_inbox_agent_introduction.DEFAULT_TEMPLATE,
        auto_send_chat_widget=True,
        assignment_in_app_notifications=False,
        assignment_email_notifications=True,
    )
    db_session.commit()

    result = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        person_id=agent,
    )

    assert result.kind == "assigned"
    assert db_session.query(AdminNotification).count() == 0
    notifications = db_session.query(Notification).all()
    assert [row.channel for row in notifications] == [NotificationChannel.email]


def test_agent_can_opt_out_of_all_assignment_notifications(db_session):
    team = _team(db_session, "Quiet Support")
    agent = _member(db_session, team)
    conversation = _conversation(db_session)
    team_inbox_agent_introduction.update_preference(
        db_session,
        person_id=agent,
        template=team_inbox_agent_introduction.DEFAULT_TEMPLATE,
        auto_send_chat_widget=True,
        assignment_in_app_notifications=False,
        assignment_email_notifications=False,
    )
    db_session.commit()

    result = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        person_id=agent,
    )

    assert result.kind == "assigned"
    assert db_session.query(Notification).count() == 0
    assert db_session.query(AdminNotification).count() == 0


def test_auto_assignment_uses_durable_round_robin_cursor(db_session):
    team = _team(db_session, "Round Robin")
    first_agent = _member(db_session, team)
    second_agent = _member(db_session, team)
    first = _conversation(db_session)
    second = _conversation(db_session)
    db_session.commit()

    first_result = team_inbox_assignment.assign_conversation_to_available_agent(
        db_session,
        conversation=first,
        service_team_id=team.id,
    )
    second_result = team_inbox_assignment.assign_conversation_to_available_agent(
        db_session,
        conversation=second,
        service_team_id=team.id,
    )
    db_session.commit()

    assert {first_result.assigned_person_id, second_result.assigned_person_id} == {
        str(first_agent),
        str(second_agent),
    }


def test_assign_conversation_to_me_requires_team_membership(db_session):
    team = _team(db_session, "Support")
    user, _person = add_bound_staff_user(db_session)
    conversation = _conversation(db_session)
    conversation.primary_service_team_id = team.id
    db_session.commit()

    with pytest.raises(
        team_inbox_commands.InboxCommandError,
        match="active member of the target team",
    ):
        team_inbox_commands.assign_conversation_to_me(
            db_session,
            conversation_id=conversation.id,
            actor_person_id=user.id,
        )
    assert db_session.query(InboxConversationAssignment).count() == 0


def test_assign_conversation_to_me_replays_existing_active_assignment(db_session):
    team = _team(db_session, "Support")
    user_id = _member(db_session, team)
    conversation = _conversation(db_session)
    conversation.primary_service_team_id = team.id
    db_session.commit()

    first = team_inbox_commands.assign_conversation_to_me(
        db_session,
        conversation_id=conversation.id,
        actor_person_id=user_id,
    )
    second = team_inbox_commands.assign_conversation_to_me(
        db_session,
        conversation_id=conversation.id,
        actor_person_id=user_id,
    )
    db_session.commit()

    assert first.message == "Assigned conversation to you."
    assert second.message == "Assigned conversation to you."
    assignments = db_session.query(InboxConversationAssignment).all()
    events = db_session.query(InboxRoutingEvent).all()
    assert len(assignments) == 1
    assert assignments[0].person_id == user_id
    assert assignments[0].service_team_id == team.id
    assert assignments[0].is_active is True
    assert len(events) == 1


def test_self_assignment_cannot_bypass_capacity(db_session):
    team = _team(db_session, "Support")
    user_id = _member(db_session, team, max_concurrent=1)
    existing = _conversation(db_session)
    target = _conversation(db_session)
    target.primary_service_team_id = team.id
    db_session.add(
        InboxConversationAssignment(
            conversation_id=existing.id,
            service_team_id=team.id,
            person_id=user_id,
            is_active=True,
        )
    )
    db_session.commit()

    with pytest.raises(
        team_inbox_commands.InboxCommandError,
        match=r"Agent is at capacity \(1 of 1 active conversations\)",
    ):
        team_inbox_commands.assign_conversation_to_me(
            db_session,
            conversation_id=target.id,
            actor_person_id=user_id,
        )
    assert db_session.query(InboxConversationAssignment).count() == 1


def test_direct_agent_assignment_still_requires_team_membership(db_session):
    team = _team(db_session, "Support")
    user, _person = add_bound_staff_user(db_session)
    conversation = _conversation(db_session)
    db_session.commit()

    result = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        person_id=user.id,
    )

    assert result.kind == "invalid_agent"
    assert result.reason == "person_id must be an active member of the target team"


def test_manual_assignment_rejects_offline_team_member(db_session):
    team = _team(db_session, "Support")
    offline_agent = _member(
        db_session, team, status=InboxAgentPresenceStatus.offline.value
    )
    conversation = _conversation(db_session)
    db_session.commit()

    result = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
        person_id=offline_agent,
    )

    assert result.kind == "agent_unavailable"
    assert result.reason == (
        "Agent is not currently available for assignment (status: offline)."
    )
    assert db_session.query(InboxConversationAssignment).count() == 0


def test_manual_assignment_reports_exact_agent_capacity(db_session):
    team = _team(db_session, "Support")
    full_agent = _member(db_session, team, max_concurrent=1)
    existing = _conversation(db_session)
    target = _conversation(db_session)
    db_session.add(
        InboxConversationAssignment(
            conversation_id=existing.id,
            service_team_id=team.id,
            person_id=full_agent,
            is_active=True,
        )
    )
    db_session.commit()

    result = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=target,
        service_team_id=team.id,
        person_id=full_agent,
    )

    assert result.kind == "agent_unavailable"
    assert result.reason == "Agent is at capacity (1 of 1 active conversations)."
    assert db_session.query(InboxConversationAssignment).count() == 1


def test_agent_at_nine_of_ten_receives_exactly_one_more(db_session):
    team = _team(db_session, "Support")
    agent = _member(db_session, team, max_concurrent=10)
    for _index in range(9):
        existing = _conversation(db_session)
        db_session.add(
            InboxConversationAssignment(
                conversation_id=existing.id,
                service_team_id=team.id,
                person_id=agent,
                is_active=True,
            )
        )
    first_target = _conversation(db_session)
    second_target = _conversation(db_session)
    db_session.flush()

    accepted = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=first_target,
        service_team_id=team.id,
        person_id=agent,
    )
    rejected = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=second_target,
        service_team_id=team.id,
        person_id=agent,
    )

    assert accepted.kind == "assigned"
    assert rejected.kind == "agent_unavailable"
    assert rejected.reason == "Agent is at capacity (10 of 10 active conversations)."
    assert (
        db_session.query(InboxConversationAssignment)
        .filter(InboxConversationAssignment.person_id == agent)
        .filter(InboxConversationAssignment.is_active.is_(True))
        .count()
        == 10
    )


def test_manual_assignment_cannot_jump_a_team_queue(db_session):
    team = _team(db_session, "Support")
    agent = _member(db_session, team)
    first = _conversation(db_session)
    second = _conversation(db_session)
    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    team_inbox_assignment.queue_conversation_for_team(
        db_session,
        conversation=first,
        service_team_id=team.id,
        now=now,
    )
    team_inbox_assignment.queue_conversation_for_team(
        db_session,
        conversation=second,
        service_team_id=team.id,
        now=now + timedelta(seconds=1),
    )

    rejected = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=second,
        service_team_id=team.id,
        person_id=agent,
        now=now + timedelta(minutes=1),
    )
    accepted = team_inbox_assignment.assign_conversation_to_agent(
        db_session,
        conversation=first,
        service_team_id=team.id,
        person_id=agent,
        now=now + timedelta(minutes=1),
    )

    assert rejected.kind == "queue_order_conflict"
    assert accepted.kind == "assigned"
    assignments = db_session.query(InboxConversationAssignment).all()
    assert [row.conversation_id for row in assignments] == [first.id]


def test_admin_capacity_setting_is_consumed_by_assignment_runtime(db_session):
    spec = settings_spec.get_spec(
        SettingDomain.comms,
        "inbox_agent_default_max_concurrent_conversations",
    )
    assert spec is not None
    assert spec.label == "Default active Inbox conversations per agent"
    assert spec.default == 10
    assert (spec.min_value, spec.max_value) == (1, 100)
    service = settings_spec.DOMAIN_SETTINGS_SERVICE[SettingDomain.comms]

    errors = web_system_settings_forms.upsert_settings_from_specs(
        db=db_session,
        form={spec.key: "25"},
        specs=[spec],
        service=service,
        context=CommandContext.system(
            actor="pytest:settings-admin",
            scope="control:settings:write",
            reason="test admin capacity setting update",
        ),
    )

    assert errors == []
    assert (
        team_inbox_assignment.resolve_default_max_concurrent_conversations(db_session)
        == 25
    )


def test_integer_admin_settings_use_text_storage_shape():
    spec = settings_spec.get_spec(SettingDomain.billing, "topup_max_amount")
    assert spec is not None

    assert settings_spec.normalize_for_db(spec, 999999999) == ("999999999", None)


def test_assign_conversation_queues_when_no_agent_available(db_session):
    team = _team(db_session)
    _member(db_session, team, status=InboxAgentPresenceStatus.away.value)
    conversation = _conversation(db_session)
    db_session.commit()

    result = team_inbox_assignment.assign_conversation_to_available_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
    )
    db_session.commit()

    assert result.kind == "queued"
    assert result.reason == "no_available_agent"
    assert conversation.primary_service_team_id == team.id
    assert (
        db_session.query(InboxConversationTeam).one().role == InboxTeamRole.owner.value
    )
    assert db_session.query(InboxConversationAssignment).count() == 0
    event = db_session.query(InboxRoutingEvent).one()
    assert event.event_type is InboxRoutingEventType.auto_assignment_declined
    assert event.decision_mode is InboxRoutingDecisionMode.automatic


def test_stale_online_presence_is_not_available_for_auto_assignment(db_session):
    team = _team(db_session)
    agent = _member(db_session, team)
    presence = (
        db_session.query(InboxAgentPresence)
        .filter(InboxAgentPresence.person_id == agent)
        .one()
    )
    stale_at = datetime.now(UTC) - timedelta(seconds=91)
    presence.last_seen_at = stale_at
    db_session.query(InboxAgentPresenceSessionLease).filter(
        InboxAgentPresenceSessionLease.person_id == agent
    ).one().last_seen_at = stale_at
    conversation = _conversation(db_session)
    db_session.commit()

    result = team_inbox_assignment.assign_conversation_to_available_agent(
        db_session,
        conversation=conversation,
        service_team_id=team.id,
    )

    assert result.kind == "queued"
    assert result.reason == "no_available_agent"


def test_effective_online_presence_stays_online_until_freshness_expires(db_session):
    observed_at = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    presence = InboxAgentPresence(
        person_id=uuid4(),
        status=InboxAgentPresenceStatus.online.value,
        manual_override_status=InboxAgentPresenceStatus.online.value,
        last_seen_at=observed_at,
    )

    assert (
        team_inbox_assignment.effective_presence_status(
            presence, now=observed_at + timedelta(seconds=90)
        )
        == InboxAgentPresenceStatus.online.value
    )
    assert (
        team_inbox_assignment.effective_presence_status(
            presence, now=observed_at + timedelta(seconds=91)
        )
        == InboxAgentPresenceStatus.offline.value
    )


def test_repeated_online_presence_update_refreshes_freshness(db_session):
    team = _team(db_session)
    agent = _member(db_session, team)
    presence = (
        db_session.query(InboxAgentPresence)
        .filter(InboxAgentPresence.person_id == agent)
        .one()
    )
    observed_at = datetime.now(UTC)

    updated = team_inbox_assignment.set_agent_presence(
        db_session,
        person_id=agent,
        status=InboxAgentPresenceStatus.online.value,
        now=observed_at,
    )

    assert updated.last_seen_at == observed_at
    assert presence.last_seen_at is not None
    assert presence.last_seen_at.replace(tzinfo=UTC) == observed_at


def test_set_agent_presence_records_effective_transition_when_online_is_stale(
    db_session,
):
    person_id = uuid4()
    observed_at = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    presence = InboxAgentPresence(
        person_id=person_id,
        status=InboxAgentPresenceStatus.online.value,
        manual_override_status=InboxAgentPresenceStatus.online.value,
        last_seen_at=observed_at - timedelta(seconds=91),
    )
    db_session.add(presence)
    db_session.flush()

    updated = team_inbox_assignment.set_agent_presence(
        db_session,
        person_id=person_id,
        status=InboxAgentPresenceStatus.online.value,
        now=observed_at,
    )

    assert updated.status == InboxAgentPresenceStatus.online.value
    assert updated.manual_override_status == InboxAgentPresenceStatus.online.value
    assert updated.last_seen_at == observed_at
    event = db_session.query(InboxAgentPresenceEvent).one()
    assert event.previous_status == InboxAgentPresenceStatus.offline.value
    assert event.status == InboxAgentPresenceStatus.online.value


def test_reply_activity_refreshes_only_manually_online_presence(db_session):
    online_person_id = uuid4()
    away_person_id = uuid4()
    original_seen_at = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    reply_seen_at = original_seen_at + timedelta(minutes=20)
    online_presence = InboxAgentPresence(
        person_id=online_person_id,
        status=InboxAgentPresenceStatus.online.value,
        manual_override_status=InboxAgentPresenceStatus.online.value,
        last_seen_at=original_seen_at,
    )
    away_presence = InboxAgentPresence(
        person_id=away_person_id,
        status=InboxAgentPresenceStatus.away.value,
        manual_override_status=InboxAgentPresenceStatus.away.value,
        last_seen_at=original_seen_at,
    )
    db_session.add_all([online_presence, away_presence])
    db_session.flush()

    team_inbox_assignment.record_agent_reply_activity(
        db_session,
        person_id=online_person_id,
        now=reply_seen_at,
    )
    team_inbox_assignment.record_agent_reply_activity(
        db_session,
        person_id=away_person_id,
        now=reply_seen_at,
    )

    assert online_presence.last_seen_at == reply_seen_at
    assert away_presence.last_seen_at == original_seen_at


def _presence_heartbeat_command(
    system_user_id,
    *,
    auth_session_id,
    observed_at: datetime,
) -> team_inbox_assignment.AgentPresenceHeartbeatCommand:
    return team_inbox_assignment.AgentPresenceHeartbeatCommand(
        context=CommandContext.system(
            actor=f"system-user:{system_user_id}",
            scope="test:team-inbox-presence-heartbeat",
            reason="test authenticated visible Inbox activity",
        ),
        system_user_id=system_user_id,
        auth_session_id=auth_session_id,
        observed_at=observed_at,
    )


def test_presence_heartbeat_creates_online_presence_for_active_staff(db_session):
    system_user, _person = add_bound_staff_user(db_session)
    system_user_id = system_user.id
    observed_at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    auth_session = _staff_session(db_session, system_user, now=observed_at)
    db_session.commit()

    outcome = team_inbox_assignment.refresh_agent_presence(
        db_session,
        command=_presence_heartbeat_command(
            system_user_id,
            auth_session_id=auth_session.id,
            observed_at=observed_at,
        ),
    )

    presence = db_session.query(InboxAgentPresence).one()
    event = db_session.query(InboxAgentPresenceEvent).one()
    assert outcome.status is InboxAgentPresenceStatus.online
    assert (
        outcome.disposition
        is team_inbox_assignment.AgentPresenceHeartbeatDisposition.refreshed
    )
    assert presence.status == InboxAgentPresenceStatus.online.value
    assert presence.manual_override_status is None
    assert presence.last_seen_at is not None
    assert presence.last_seen_at.replace(tzinfo=UTC) == observed_at
    assert (
        event.reason_code
        == team_inbox_assignment.InboxPresenceReason.authenticated_inbox_activity
    )


def test_presence_heartbeat_revives_stale_selected_online_presence(db_session):
    system_user, _person = add_bound_staff_user(db_session)
    system_user_id = system_user.id
    observed_at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    auth_session = _staff_session(db_session, system_user, now=observed_at)
    presence = InboxAgentPresence(
        person_id=system_user_id,
        status=InboxAgentPresenceStatus.online.value,
        manual_override_status=InboxAgentPresenceStatus.online.value,
        last_seen_at=observed_at - timedelta(minutes=31),
    )
    db_session.add(presence)
    db_session.commit()

    outcome = team_inbox_assignment.refresh_agent_presence(
        db_session,
        command=_presence_heartbeat_command(
            system_user_id,
            auth_session_id=auth_session.id,
            observed_at=observed_at,
        ),
    )

    assert outcome.status is InboxAgentPresenceStatus.online
    assert presence.manual_override_status == InboxAgentPresenceStatus.online.value
    assert presence.last_seen_at is not None
    assert presence.last_seen_at.replace(tzinfo=UTC) == observed_at
    event = db_session.query(InboxAgentPresenceEvent).one()
    assert event.previous_status == InboxAgentPresenceStatus.offline.value
    assert event.status == InboxAgentPresenceStatus.online.value


@pytest.mark.parametrize(
    "selected_status",
    [
        InboxAgentPresenceStatus.away,
        InboxAgentPresenceStatus.on_break,
        InboxAgentPresenceStatus.offline,
    ],
)
def test_presence_heartbeat_preserves_explicit_unavailable_state(
    db_session,
    selected_status,
):
    system_user, _person = add_bound_staff_user(db_session)
    system_user_id = system_user.id
    original_seen_at = datetime(2026, 9, 10, 11, 0, tzinfo=UTC)
    observed_at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    auth_session = _staff_session(db_session, system_user, now=observed_at)
    presence = InboxAgentPresence(
        person_id=system_user_id,
        status=selected_status.value,
        manual_override_status=selected_status.value,
        last_seen_at=original_seen_at,
    )
    db_session.add(presence)
    db_session.commit()

    outcome = team_inbox_assignment.refresh_agent_presence(
        db_session,
        command=_presence_heartbeat_command(
            system_user_id,
            auth_session_id=auth_session.id,
            observed_at=observed_at,
        ),
    )

    assert outcome.status is selected_status
    assert (
        outcome.disposition
        is team_inbox_assignment.AgentPresenceHeartbeatDisposition.explicit_unavailable
    )
    assert presence.last_seen_at is not None
    assert presence.last_seen_at.replace(tzinfo=UTC) == original_seen_at
    assert db_session.query(InboxAgentPresenceEvent).count() == 0


def test_presence_heartbeat_rejects_inactive_staff_without_writing(db_session):
    system_user, _person = add_bound_staff_user(db_session)
    system_user_id = system_user.id
    observed_at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    auth_session = _staff_session(db_session, system_user, now=observed_at)
    system_user.is_active = False
    db_session.commit()

    outcome = team_inbox_assignment.refresh_agent_presence(
        db_session,
        command=_presence_heartbeat_command(
            system_user_id,
            auth_session_id=auth_session.id,
            observed_at=observed_at,
        ),
    )

    assert outcome.status is InboxAgentPresenceStatus.offline
    assert (
        outcome.disposition
        is team_inbox_assignment.AgentPresenceHeartbeatDisposition.inactive_principal
    )
    assert db_session.query(InboxAgentPresence).count() == 0
    assert db_session.query(InboxAgentPresenceEvent).count() == 0


def test_presence_heartbeat_rejects_wrong_or_revoked_staff_session(db_session):
    system_user, _person = add_bound_staff_user(db_session)
    other_user, _other_person = add_bound_staff_user(db_session)
    observed_at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    wrong_session = _staff_session(db_session, other_user, now=observed_at)
    revoked_session = _staff_session(db_session, system_user, now=observed_at)
    revoked_session.status = SessionStatus.revoked
    revoked_session.revoked_at = observed_at
    db_session.commit()

    for auth_session_id in (wrong_session.id, revoked_session.id):
        outcome = team_inbox_assignment.refresh_agent_presence(
            db_session,
            command=_presence_heartbeat_command(
                system_user.id,
                auth_session_id=auth_session_id,
                observed_at=observed_at,
            ),
        )
        assert (
            outcome.disposition
            is team_inbox_assignment.AgentPresenceHeartbeatDisposition.invalid_session
        )

    assert db_session.query(InboxAgentPresenceSessionLease).count() == 0
    assert db_session.query(InboxAgentPresence).count() == 0


def test_auto_offline_requires_a_fresh_active_session_lease(db_session):
    system_user, _person = add_bound_staff_user(db_session)
    observed_at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    auth_session = _staff_session(db_session, system_user, now=observed_at)
    db_session.add(
        InboxAgentPresence(
            person_id=system_user.id,
            status=InboxAgentPresenceStatus.online.value,
            last_seen_at=observed_at,
            auto_offline=True,
        )
    )
    db_session.add(
        InboxAgentPresenceSessionLease(
            person_id=system_user.id,
            auth_session_id=auth_session.id,
            last_seen_at=observed_at,
        )
    )
    db_session.commit()

    fresh = team_inbox_assignment.agent_availability_snapshots(
        db_session,
        (system_user.id,),
        now=observed_at + timedelta(seconds=90),
    )
    stale = team_inbox_assignment.agent_availability_snapshots(
        db_session,
        (system_user.id,),
        now=observed_at + timedelta(seconds=91),
    )

    assert fresh[system_user.id].presence_status is InboxAgentPresenceStatus.online
    assert fresh[system_user.id].assignment_eligible is True
    assert stale[system_user.id].presence_status is InboxAgentPresenceStatus.offline
    assert stale[system_user.id].assignment_eligible is False


@pytest.mark.parametrize("terminal_state", ["revoked", "expired"])
def test_disabling_auto_offline_still_requires_a_live_bound_session(
    db_session, terminal_state
):
    system_user, _person = add_bound_staff_user(db_session)
    observed_at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    auth_session = _staff_session(db_session, system_user, now=observed_at)
    db_session.add(
        InboxAgentPresence(
            person_id=system_user.id,
            status=InboxAgentPresenceStatus.online.value,
            last_seen_at=observed_at - timedelta(minutes=5),
            auto_offline=True,
        )
    )
    db_session.add(
        InboxAgentPresenceSessionLease(
            person_id=system_user.id,
            auth_session_id=auth_session.id,
            last_seen_at=observed_at - timedelta(minutes=5),
        )
    )
    db_session.commit()

    outcome = team_inbox_commands.set_agent_presence_preference(
        db_session,
        command=team_inbox_assignment.AgentPresencePreferenceCommand(
            context=CommandContext.system(
                actor=f"system-user:{system_user.id}",
                scope="test:team-inbox-presence-preference",
                reason="test explicit auto-offline preference",
            ),
            system_user_id=system_user.id,
            auto_offline=False,
            observed_at=observed_at,
        ),
    )

    presence = db_session.query(InboxAgentPresence).one()
    event = db_session.query(InboxAgentPresenceEvent).one()
    assert outcome.auto_offline is False
    assert outcome.previous_status is InboxAgentPresenceStatus.offline
    assert outcome.status is InboxAgentPresenceStatus.online
    assert presence.auto_offline is False
    assert (
        event.reason_code == team_inbox_assignment.InboxPresenceReason.preference_change
    )
    available = team_inbox_assignment.agent_availability_snapshots(
        db_session,
        (system_user.id,),
        now=observed_at,
    )
    assert available[system_user.id].presence_status is InboxAgentPresenceStatus.online
    assert available[system_user.id].assignment_eligible is True

    if terminal_state == "revoked":
        auth_session.status = SessionStatus.revoked
        auth_session.revoked_at = observed_at
    else:
        auth_session.expires_at = observed_at
    db_session.commit()

    unavailable = team_inbox_assignment.agent_availability_snapshots(
        db_session,
        (system_user.id,),
        now=observed_at + timedelta(seconds=1),
    )
    assert (
        unavailable[system_user.id].presence_status is InboxAgentPresenceStatus.offline
    )
    assert unavailable[system_user.id].assignment_eligible is False


def test_set_agent_presence_creates_manual_override(db_session):
    person_id = uuid4()

    presence = team_inbox_assignment.set_agent_presence(
        db_session,
        person_id=person_id,
        status=InboxAgentPresenceStatus.away.value,
    )
    db_session.commit()

    assert presence.person_id == person_id
    assert presence.status == InboxAgentPresenceStatus.away.value
    assert presence.manual_override_status == InboxAgentPresenceStatus.away.value
    assert presence.last_seen_at is not None


def test_staff_sign_in_preserves_explicit_unavailable_override(db_session):
    system_user, _person = add_bound_staff_user(db_session)
    signed_in_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    auth_session = _staff_session(db_session, system_user, now=signed_in_at)
    db_session.add(
        InboxAgentPresence(
            person_id=system_user.id,
            status=InboxAgentPresenceStatus.away.value,
            manual_override_status=InboxAgentPresenceStatus.away.value,
            last_seen_at=signed_in_at - timedelta(minutes=5),
        )
    )
    db_session.flush()

    outcome = team_inbox_assignment.record_agent_signed_in_presence(
        db_session,
        command=team_inbox_assignment.AgentSignedInPresenceCommand(
            system_user_id=system_user.id,
            auth_session_id=auth_session.id,
            signed_in_at=signed_in_at,
        ),
    )

    presence = db_session.query(InboxAgentPresence).one()
    lease = db_session.query(InboxAgentPresenceSessionLease).one()
    assert outcome.status is InboxAgentPresenceStatus.away
    assert outcome.transition_recorded is False
    assert presence.status == InboxAgentPresenceStatus.away.value
    assert presence.manual_override_status == InboxAgentPresenceStatus.away.value
    assert presence.last_seen_at == signed_in_at - timedelta(minutes=5)
    assert lease.auth_session_id == auth_session.id
    assert lease.last_seen_at == signed_in_at
    assert db_session.query(InboxAgentPresenceEvent).count() == 0


def test_staff_sign_in_locks_only_team_inbox_presence():
    sign_in_source = inspect.getsource(
        team_inbox_assignment.record_agent_signed_in_presence
    )
    presence_source = inspect.getsource(team_inbox_assignment.set_agent_presence)

    assert ".with_for_update()" not in sign_in_source
    assert ".with_for_update()" in presence_source


def test_staff_sign_in_refreshes_online_presence_without_false_transition(db_session):
    system_user, _person = add_bound_staff_user(db_session)
    signed_in_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    auth_session = _staff_session(db_session, system_user, now=signed_in_at)
    db_session.add(
        InboxAgentPresence(
            person_id=system_user.id,
            status=InboxAgentPresenceStatus.online.value,
            manual_override_status=InboxAgentPresenceStatus.online.value,
            last_seen_at=signed_in_at - timedelta(minutes=5),
        )
    )
    db_session.flush()

    outcome = team_inbox_assignment.record_agent_signed_in_presence(
        db_session,
        command=team_inbox_assignment.AgentSignedInPresenceCommand(
            system_user_id=system_user.id,
            auth_session_id=auth_session.id,
            signed_in_at=signed_in_at,
        ),
    )

    presence = db_session.query(InboxAgentPresence).one()
    assert outcome.transition_recorded is False
    assert presence.status == InboxAgentPresenceStatus.online.value
    assert presence.manual_override_status is None
    assert presence.last_seen_at == signed_in_at
    assert db_session.query(InboxAgentPresenceEvent).count() == 0
