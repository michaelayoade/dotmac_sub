from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app.api import support as support_api
from app.models.notification import Notification
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamCapability,
    ServiceTeamCapabilityDefinition,
    ServiceTeamMember,
    ServiceTeamType,
)
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationQueueEntry,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
    InboxQueueEntryStatus,
    InboxTeamRole,
    InboxTeamSource,
)
from app.services import (
    service_team_composition,
    team_inbox_operations,
    team_inbox_projection,
    team_inbox_read,
)
from app.web.admin import inbox as admin_inbox
from tests.staff_identity_fixtures import add_bound_staff_user
from tests.team_inbox_presence_fixtures import add_active_presence_lease


def _team(db_session, name: str, team_type: str = ServiceTeamType.support.value):
    capabilities = service_team_composition.LEGACY_TYPE_CAPABILITIES[team_type]
    for capability in capabilities:
        contract = service_team_composition.CAPABILITY_CONTRACTS[capability]
        if db_session.get(ServiceTeamCapabilityDefinition, capability.value) is None:
            db_session.add(
                ServiceTeamCapabilityDefinition(
                    key=capability.value,
                    display_name=contract.display_name,
                    contract_owner=contract.contract_owner,
                    contract_version=contract.contract_version,
                    description=f"Test definition for {capability.value}",
                    is_active=True,
                )
            )
    team = ServiceTeam(name=name, team_type=team_type)
    db_session.add(team)
    db_session.flush()
    for capability in capabilities:
        db_session.add(
            ServiceTeamCapability(
                team_id=team.id,
                capability_key=capability.value,
                is_active=True,
            )
        )
    db_session.flush()
    return team


def _conversation(
    db_session,
    team: ServiceTeam,
    *,
    subject: str,
    status: str = InboxConversationStatus.open.value,
    channel_type: str = "email",
    contact_address: str = "customer@example.com",
    first_at: datetime | None = None,
) -> InboxConversation:
    first_at = first_at or datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    conversation = InboxConversation(
        channel_type=channel_type,
        status=status,
        subject=subject,
        contact_address=contact_address,
        primary_service_team_id=team.id,
        first_message_at=first_at,
        last_message_at=first_at,
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxConversationTeam(
            conversation_id=conversation.id,
            service_team_id=team.id,
            role=InboxTeamRole.owner.value,
            source=InboxTeamSource.routing_rule.value,
            is_active=True,
        )
    )
    db_session.flush()
    return conversation


def _online_reply_agent(db_session, team: ServiceTeam):
    user, person = add_bound_staff_user(db_session)
    db_session.add_all(
        [
            ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True),
            InboxAgentPresence(
                person_id=user.id,
                status="online",
                manual_override_status="online",
                last_seen_at=datetime.now(UTC),
            ),
        ]
    )
    add_active_presence_lease(db_session, system_user_id=user.id)
    db_session.flush()
    return user


def _message(
    db_session,
    conversation: InboxConversation,
    *,
    direction: str,
    body: str,
    at: datetime,
) -> InboxMessage:
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=conversation.channel_type,
        direction=direction,
        subject=conversation.subject,
        body=body,
        from_address="customer@example.com"
        if direction == InboxMessageDirection.inbound.value
        else "support@dotmac.io",
        to_addresses=["support@dotmac.io"]
        if direction == InboxMessageDirection.inbound.value
        else ["customer@example.com"],
        received_at=at if direction == InboxMessageDirection.inbound.value else None,
        sent_at=at if direction == InboxMessageDirection.outbound.value else None,
    )
    db_session.add(message)
    db_session.flush()
    return message


def test_list_conversations_filters_by_team_search_and_response_need(db_session):
    support = _team(db_session, "Support")
    billing = _team(db_session, "Billing", ServiceTeamType.billing.value)
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    needs_reply = _conversation(
        db_session,
        support,
        subject="Router offline",
        contact_address="ada@example.com",
        first_at=base,
    )
    _message(
        db_session,
        needs_reply,
        direction=InboxMessageDirection.inbound.value,
        body="Router is down",
        at=base,
    )
    answered = _conversation(
        db_session,
        support,
        subject="Install date",
        contact_address="install@example.com",
        first_at=base,
    )
    _message(
        db_session,
        answered,
        direction=InboxMessageDirection.inbound.value,
        body="When is install?",
        at=base,
    )
    _message(
        db_session,
        answered,
        direction=InboxMessageDirection.outbound.value,
        body="Tomorrow.",
        at=base.replace(hour=9),
    )
    billing_case = _conversation(
        db_session,
        billing,
        subject="Invoice copy",
        contact_address="billing@example.com",
        first_at=base,
    )
    _message(
        db_session,
        billing_case,
        direction=InboxMessageDirection.inbound.value,
        body="Need invoice",
        at=base,
    )
    db_session.commit()

    result = team_inbox_read.list_conversations(
        db_session,
        service_team_id=support.id,
        search="router",
        needs_response=True,
    )

    assert result.count == 1
    assert [item.id for item in result.items] == [str(needs_reply.id)]
    assert result.items[0].primary_service_team_name == "Support"
    assert result.items[0].primary_service_team_capabilities == ("customer_support",)
    assert result.items[0].latest_message_body == "Router is down"
    assert result.items[0].needs_response is True


def test_queue_position_query_hydrates_only_requested_entries(db_session):
    team = _team(db_session, "Ranked queue")
    start = datetime(2026, 11, 12, 6, 0, tzinfo=UTC)
    conversations = [
        _conversation(
            db_session,
            team,
            subject=f"Queued conversation {index}",
            first_at=start + timedelta(minutes=index),
        )
        for index in range(4)
    ]
    db_session.add_all(
        [
            InboxConversationQueueEntry(
                conversation_id=conversation.id,
                service_team_id=team.id,
                queue_position=index + 1,
                status=(
                    InboxQueueEntryStatus.cancelled.value
                    if index == 1
                    else InboxQueueEntryStatus.queued.value
                ),
                entered_at=start + timedelta(minutes=index),
            )
            for index, conversation in enumerate(conversations)
        ]
    )
    db_session.flush()

    evidence = team_inbox_read._queue_positions_by_conversation(
        db_session,
        conversation_ids=(conversations[3].id,),
        service_team_ids=(team.id,),
    )

    assert tuple(evidence) == (conversations[3].id,)
    assert evidence[conversations[3].id].current_visible_position == 3
    assert evidence[conversations[3].id].entry.conversation_id == conversations[3].id


def test_response_cohort_evidence_uses_one_database_statement(db_session):
    team = _team(db_session, "Response evidence")
    conversation = _conversation(
        db_session,
        team,
        subject="Follow-up",
    )
    start = datetime(2026, 11, 12, 7, 0, tzinfo=UTC)
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        body="Initial question",
        at=start,
    )
    reply = _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        body="Agent answer",
        at=start + timedelta(minutes=1),
    )
    reply.metadata_ = {
        "sent_by_person_id": str(uuid4()),
        "delivery_status": "delivered",
    }
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        body="Customer follow-up",
        at=start + timedelta(minutes=2),
    )
    db_session.flush()
    statements: list[str] = []

    def capture_statement(*args):
        statements.append(str(args[2]))

    event.listen(db_session.bind, "before_cursor_execute", capture_statement)
    try:
        cohorts = team_inbox_read._response_cohorts_by_conversation(
            db_session,
            (conversation,),
            ticketed_conversation_ids=set(),
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture_statement)

    assert (
        cohorts[conversation.id] is team_inbox_read.InboxResponseCohort.needs_attention
    )
    assert len(statements) == 1


def test_assignment_sidebar_cohorts_share_one_aggregate(db_session):
    team = _team(db_session, "Sidebar cohorts")
    actor = _online_reply_agent(db_session, team)
    assigned = _conversation(db_session, team, subject="Assigned")
    queued = _conversation(db_session, team, subject="Queued")
    db_session.add_all(
        [
            InboxConversationAssignment(
                conversation_id=assigned.id,
                service_team_id=team.id,
                person_id=actor.id,
                is_active=True,
            ),
            InboxConversationQueueEntry(
                conversation_id=queued.id,
                service_team_id=team.id,
                queue_position=1,
                status=InboxQueueEntryStatus.queued.value,
                entered_at=datetime(2026, 11, 12, 8, 0, tzinfo=UTC),
            ),
        ]
    )
    db_session.flush()

    statements: list[str] = []

    def capture_statement(*args):
        statements.append(str(args[2]))

    event.listen(db_session.bind, "before_cursor_execute", capture_statement)
    try:
        counts = team_inbox_read.assignment_cohort_counts(
            db_session,
            actor_person_id=actor.id,
            actor_team_ids=(team.id,),
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture_statement)

    assert len(statements) == 1
    assert "last_qualifying_inbound_at" in statements[0]
    assert "FROM inbox_messages" not in statements[0]
    assert counts.all == team_inbox_read.queue_conversation_count(db_session)
    assert counts.queued == team_inbox_read.queued_conversation_count(db_session)
    assert counts.assigned_to_actor == team_inbox_read.assigned_conversation_count(
        db_session,
        assigned_person_id=actor.id,
    )
    assert counts.actor_teams == 2
    assert counts.ai_handling == team_inbox_read.ai_handling_conversation_count(
        db_session
    )


class _StatementTimeout:
    sqlstate = "57014"


def test_admin_inbox_timeout_renders_without_assignment_totals(db_session, monkeypatch):
    support = _team(db_session, "Timeout support")
    _conversation(db_session, support, subject="Still visible")
    db_session.commit()
    captured: dict[str, object] = {}

    def timeout(*_args, **_kwargs):
        raise OperationalError("SELECT assignment counts", {}, _StatementTimeout())

    def fake_template_response(
        template_name: str,
        context: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        captured["template_name"] = template_name
        captured["headers"] = headers
        return context

    monkeypatch.setattr(team_inbox_read, "assignment_cohort_counts", timeout)
    monkeypatch.setattr(
        admin_inbox.templates,
        "TemplateResponse",
        fake_template_response,
    )

    context = admin_inbox.team_inbox_queue(
        request=SimpleNamespace(state=SimpleNamespace()),
        search=None,
        status=None,
        channel_type=None,
        service_team_id=None,
        assigned_person_id=None,
        needs_response=False,
        contact_resolution_status=None,
        priority_at_most=None,
        muted=None,
        snoozed=None,
        open_only=False,
        unassigned=False,
        sort_by=None,
        sort_dir=None,
        page=1,
        per_page=25,
        db=db_session,
    )

    assert captured["template_name"] == "admin/inbox/index.html"
    assert context["rows"][0].subject == "Still visible"
    assert (
        context["assignment_counts"].state
        is team_inbox_projection.InboxAssignmentCountsState.unavailable
    )


def test_assignment_count_fallback_does_not_hide_other_database_errors(
    db_session, monkeypatch
):
    class ConnectionFailure:
        sqlstate = "08006"

    def unavailable(*_args, **_kwargs):
        raise OperationalError("SELECT assignment counts", {}, ConnectionFailure())

    monkeypatch.setattr(team_inbox_read, "assignment_cohort_counts", unavailable)
    queue_metrics = team_inbox_operations.InboxQueueMetrics(
        total_open=0,
        needs_response=0,
        failed_outbound=0,
        unassigned_open=0,
        muted_open=0,
        snoozed_open=0,
    )

    with pytest.raises(OperationalError):
        team_inbox_projection._assignment_counts(
            db_session,
            actor_person_id=None,
            queue_metrics=queue_metrics,
            needs_attention=0,
        )


def test_search_maps_historical_message_hit_to_current_continuous_episode(
    db_session,
):
    support = _team(db_session, "Continuous search")
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    historical = _conversation(
        db_session,
        support,
        subject="Earlier support episode",
        status=InboxConversationStatus.resolved.value,
        first_at=base,
    )
    _message(
        db_session,
        historical,
        direction=InboxMessageDirection.inbound.value,
        body="The unique optical reference is ALPHA-742",
        at=base,
    )
    current = _conversation(
        db_session,
        support,
        subject="Current support episode",
        first_at=base.replace(hour=9),
    )
    current.continued_from_conversation_id = historical.id
    current.metadata_ = {"conversation_mode": "continuous"}
    _message(
        db_session,
        current,
        direction=InboxMessageDirection.inbound.value,
        body="Following up",
        at=base.replace(hour=9),
    )
    db_session.commit()

    result = team_inbox_read.list_conversations(
        db_session,
        service_team_id=support.id,
        search="ALPHA-742",
        open_only=True,
    )

    assert result.count == 1
    assert [item.id for item in result.items] == [str(current.id)]


def test_list_conversations_filters_by_contact_resolution_status(db_session):
    support = _team(db_session, "Support")
    unmatched = _conversation(
        db_session,
        support,
        subject="Instagram account",
        contact_address="ig-1",
        channel_type="instagram_dm",
    )
    unmatched.metadata_ = {"contact_resolution": {"status": "unmatched"}}
    linked = _conversation(
        db_session,
        support,
        subject="Known account",
        contact_address="ada@example.com",
    )
    linked.metadata_ = {"contact_resolution": {"status": "linked_subscriber"}}
    db_session.commit()

    result = team_inbox_read.list_conversations(
        db_session,
        contact_resolution_status="unmatched",
    )

    assert result.count == 1
    assert result.items[0].id == str(unmatched.id)
    assert result.items[0].contact_resolution_status == "unmatched"


def test_list_conversations_keeps_latest_named_provider_identity(db_session):
    support = _team(db_session, "Support")
    conversation = _conversation(
        db_session,
        support,
        subject="Provider identity",
        contact_address="provider-123",
    )
    named = _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        body="Hello",
        at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
    )
    named.metadata_ = {"from_name": "Ada Customer"}
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        body="Still there?",
        at=datetime(2026, 7, 10, 8, 1, tzinfo=UTC),
    )
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        body="Yes, how can we help?",
        at=datetime(2026, 7, 10, 8, 2, tzinfo=UTC),
    )
    db_session.commit()

    result = team_inbox_read.list_conversations(db_session)
    timeline = team_inbox_read.get_conversation_timeline(
        db_session,
        conversation.id,
        message_page=team_inbox_read.InboxTimelineMessagePageRequest(),
    )

    assert result.items[0].contact_name == "Ada Customer"
    assert result.items[0].latest_message_body == "Yes, how can we help?"
    assert timeline is not None
    assert timeline.contact_name == "Ada Customer"


def test_list_conversations_api_returns_list_response(db_session):
    support = _team(db_session, "Support")
    conversation = _conversation(db_session, support, subject="Router offline")
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        body="Down",
        at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
    )
    db_session.commit()

    response = support_api.list_inbox_conversations(
        search=None,
        status=None,
        channel_type=None,
        service_team_id=str(support.id),
        assigned_person_id=None,
        needs_response=True,
        limit=50,
        offset=0,
        db=db_session,
    )

    assert response["count"] == 1
    assert response["items"][0].id == conversation.id
    assert response["items"][0].needs_response is True


def test_admin_inbox_queue_renders_filtered_context(db_session, monkeypatch):
    support = _team(db_session, "Support")
    conversation = _conversation(db_session, support, subject="Router offline")
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        body="Down",
        at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
    )
    captured: dict[str, object] = {}

    def _fake_template_response(
        template_name: str,
        context: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        captured["template_name"] = template_name
        captured["context"] = context
        captured["headers"] = headers
        return context

    monkeypatch.setattr(
        admin_inbox.templates,
        "TemplateResponse",
        _fake_template_response,
    )
    db_session.commit()

    context = admin_inbox.team_inbox_queue(
        request=SimpleNamespace(state=SimpleNamespace()),
        search="router",
        status=None,
        channel_type=None,
        service_team_id=str(support.id),
        assigned_person_id=None,
        needs_response=True,
        contact_resolution_status=None,
        priority_at_most=None,
        muted=None,
        snoozed=None,
        open_only=False,
        unassigned=False,
        sort_by=None,
        sort_dir=None,
        page=1,
        per_page=25,
        db=db_session,
    )

    assert captured["template_name"] == "admin/inbox/index.html"
    assert captured["headers"] == admin_inbox.INBOX_HTML_RESPONSE_HEADERS
    assert context["rows"][0].id == str(conversation.id)
    assert context["search"] == "router"
    assert context["service_team_id"] == str(support.id)
    assert context["needs_response"] is True


def test_admin_inbox_detail_renders_timeline(db_session, monkeypatch):
    support = _team(db_session, "Support")
    conversation = _conversation(db_session, support, subject="Router offline")
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        body="Down",
        at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
    )
    captured: dict[str, object] = {}

    def _fake_template_response(
        template_name: str,
        context: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        captured["template_name"] = template_name
        captured["context"] = context
        captured["headers"] = headers
        captured["html"] = admin_inbox.templates.env.get_template(template_name).render(
            **context
        )
        return context

    monkeypatch.setattr(
        admin_inbox.templates,
        "TemplateResponse",
        _fake_template_response,
    )

    def _fail_full_page_context(*_args, **_kwargs):
        raise AssertionError(
            "conversation partial must not rebuild global sidebar statistics"
        )

    monkeypatch.setattr(admin_inbox, "_ctx", _fail_full_page_context)
    db_session.commit()

    context = admin_inbox.team_inbox_detail(
        conversation.id,
        request=SimpleNamespace(
            state=SimpleNamespace(),
            query_params={},
            headers={
                "hx-request": "true",
                "hx-current-url": (
                    "https://selfcare.dotmac.io/admin/inbox?"
                    "status=open&page=7&per_page=25"
                ),
            },
        ),
        db=db_session,
    )

    # HTMX list clicks render the thread+context partial into the workspace.
    assert captured["template_name"] == "admin/inbox/_conversation.html"
    assert captured["headers"] == admin_inbox.INBOX_HTML_RESPONSE_HEADERS
    assert context["timeline"].id == str(conversation.id)
    assert context["timeline"].messages[0].body == "Down"
    assert context["request"].headers["hx-request"] == "true"
    assert context["queue_return_url"] == "/admin/inbox?status=open&page=7&per_page=25"
    assert "sidebar_stats" not in context
    assert "Down" in captured["html"]


def test_admin_inbox_detail_non_htmx_redirects_to_workspace(db_session):
    support = _team(db_session, "Support")
    conversation = _conversation(db_session, support, subject="Router offline")
    db_session.commit()

    response = admin_inbox.team_inbox_detail(
        conversation.id,
        request=SimpleNamespace(state=SimpleNamespace(), query_params={}, headers={}),
        db=db_session,
    )

    # A full navigation lands in the workspace with the conversation preselected.
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/inbox?c={conversation.id}"


def test_admin_inbox_detail_reply_queues_message(db_session):
    support = _team(db_session, "Support")
    agent = _online_reply_agent(db_session, support)
    conversation = _conversation(db_session, support, subject="Router offline")
    db_session.commit()

    response = admin_inbox.team_inbox_reply(
        conversation.id,
        request=SimpleNamespace(state=SimpleNamespace(user=agent)),
        background_tasks=BackgroundTasks(),
        body_text="We are checking this now.",
        db=db_session,
    )

    assert response.status_code == 303
    assert f"/admin/inbox?c={conversation.id}" in response.headers["location"]
    notification = db_session.query(Notification).one()
    assert notification.recipient == "customer@example.com"
    timeline = team_inbox_read.get_conversation_timeline(db_session, conversation.id)
    assert timeline is not None
    assert timeline.messages[-1].body == "We are checking this now."
    assert timeline.messages[-1].metadata["body_html"] == (
        "<p>We are checking this now.</p>"
    )
    assert timeline.messages[-1].metadata["source_route"] == "admin_inbox_detail_reply"


def test_conversation_timeline_returns_teams_assignments_and_messages(db_session):
    team = _team(db_session, "Support")
    conversation = InboxConversation(
        channel_type="email",
        status=InboxConversationStatus.open.value,
        subject="Need help",
        contact_address="customer@example.com",
        primary_service_team_id=team.id,
        first_message_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        last_message_at=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxConversationTeam(
            conversation_id=conversation.id,
            service_team_id=team.id,
            role=InboxTeamRole.owner.value,
            source=InboxTeamSource.routing_rule.value,
            is_active=True,
        )
    )
    assignee_id = uuid4()
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=assignee_id,
            assigned_at=datetime(2026, 7, 10, 8, 2, tzinfo=UTC),
            is_active=True,
        )
    )
    db_session.add_all(
        [
            InboxMessage(
                conversation_id=conversation.id,
                channel_type="email",
                direction=InboxMessageDirection.inbound.value,
                subject="Need help",
                body="Router offline",
                from_address="customer@example.com",
                to_addresses=["support@dotmac.io"],
                received_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
            ),
            InboxMessage(
                conversation_id=conversation.id,
                channel_type="email",
                direction=InboxMessageDirection.outbound.value,
                subject="Re: Need help",
                body="Checking.",
                from_address="support@dotmac.io",
                to_addresses=["customer@example.com"],
                cc_addresses=["copy@example.com"],
                metadata_={"bcc": ["audit@example.com"]},
                sent_at=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
            ),
        ]
    )
    db_session.commit()

    timeline = team_inbox_read.get_conversation_timeline(db_session, conversation.id)

    assert timeline is not None
    assert timeline.id == str(conversation.id)
    assert timeline.primary_service_team_id == str(team.id)
    assert timeline.teams[0].service_team_name == "Support"
    assert timeline.teams[0].service_team_capabilities == ("customer_support",)
    assert timeline.assignments[0].person_id == str(assignee_id)
    assert [message.direction for message in timeline.messages] == [
        InboxMessageDirection.inbound.value,
        InboxMessageDirection.outbound.value,
    ]
    assert timeline.messages[0].sender is None
    assert timeline.messages[1].sender is not None
    assert timeline.messages[1].sender.display_name == "Support agent"
    assert timeline.messages[1].sender.initials == "AG"
    assert timeline.messages[1].to_addresses == ["customer@example.com"]
    assert timeline.messages[1].cc_addresses == ["copy@example.com"]
    assert timeline.messages[1].bcc_addresses == ["audit@example.com"]
    assert (
        timeline.messages[1].sender.source
        is team_inbox_read.InboxTimelineSenderSource.fallback
    )


def test_conversation_message_pages_load_ten_newest_then_ten_older(db_session):
    team = _team(db_session, "Support")
    conversation = _conversation(db_session, team, subject="Long thread")
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    for index in range(25):
        _message(
            db_session,
            conversation,
            direction=InboxMessageDirection.inbound.value,
            body=f"Message {index:02d}",
            at=base.replace(minute=index),
        )
    db_session.commit()

    newest = team_inbox_read.get_conversation_message_page(
        db_session,
        conversation_id=conversation.id,
        page=team_inbox_read.InboxTimelineMessagePageRequest(),
    )

    assert newest is not None
    assert [message.body for message in newest.messages] == [
        f"Message {index:02d}" for index in range(15, 25)
    ]
    assert newest.has_older_messages is True
    assert newest.oldest_message_id is not None

    older = team_inbox_read.get_conversation_message_page(
        db_session,
        conversation_id=conversation.id,
        page=team_inbox_read.InboxTimelineMessagePageRequest(
            before_message_id=newest.oldest_message_id
        ),
    )

    assert older is not None
    assert [message.body for message in older.messages] == [
        f"Message {index:02d}" for index in range(5, 15)
    ]
    assert older.has_older_messages is True


def test_conversation_message_page_rejects_cursor_from_another_thread(db_session):
    team = _team(db_session, "Support")
    requested = _conversation(db_session, team, subject="Requested")
    other = _conversation(db_session, team, subject="Other")
    other_message = _message(
        db_session,
        other,
        direction=InboxMessageDirection.inbound.value,
        body="Private to the other thread",
        at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
    )
    db_session.commit()

    page = team_inbox_read.get_conversation_message_page(
        db_session,
        conversation_id=requested.id,
        page=team_inbox_read.InboxTimelineMessagePageRequest(
            before_message_id=other_message.id
        ),
    )

    assert page is None


def test_paged_conversation_timeline_contains_only_latest_ten_messages(db_session):
    team = _team(db_session, "Support")
    conversation = _conversation(db_session, team, subject="Paged detail")
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    for index in range(12):
        _message(
            db_session,
            conversation,
            direction=InboxMessageDirection.inbound.value,
            body=f"Message {index:02d}",
            at=base.replace(minute=index),
        )
    db_session.commit()

    timeline = team_inbox_read.get_conversation_timeline(
        db_session,
        conversation.id,
        message_page=team_inbox_read.InboxTimelineMessagePageRequest(),
    )

    assert timeline is not None
    assert [message.body for message in timeline.messages] == [
        f"Message {index:02d}" for index in range(2, 12)
    ]
    assert timeline.has_older_messages is True
    assert timeline.oldest_message_id == timeline.messages[0].id


def test_social_comment_timeline_stays_complete_until_tree_paging_exists(db_session):
    team = _team(db_session, "Social")
    conversation = _conversation(
        db_session,
        team,
        subject="Public comment tree",
        channel_type="facebook_comment",
    )
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    for index in range(12):
        _message(
            db_session,
            conversation,
            direction=InboxMessageDirection.inbound.value,
            body=f"Comment {index:02d}",
            at=base.replace(minute=index),
        )
    db_session.commit()

    timeline = team_inbox_read.get_conversation_timeline(
        db_session,
        conversation.id,
        message_page=team_inbox_read.InboxTimelineMessagePageRequest(),
    )

    assert timeline is not None
    assert len(timeline.messages) == 12
    assert timeline.has_older_messages is False


def test_conversation_timeline_resolves_inactive_outbound_sender_identity(db_session):
    team = _team(db_session, "Support")
    conversation = _conversation(db_session, team, subject="Sender identity")
    sender = SystemUser(
        first_name="Ibrahim",
        last_name="Nurudeen",
        display_name="Ibrahim Nurudeen",
        email="inactive.inbox.sender@example.test",
        is_active=False,
    )
    db_session.add(sender)
    db_session.flush()
    message = _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        body="We are checking this now.",
        at=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
    )
    message.metadata_ = {
        "sent_by_person_id": str(sender.id),
        "delivery_status": "sent",
    }
    db_session.commit()

    timeline = team_inbox_read.get_conversation_timeline(db_session, conversation.id)

    assert timeline is not None
    projected = timeline.messages[-1].sender
    assert projected is not None
    assert projected.system_user_id == str(sender.id)
    assert projected.display_name == "Ibrahim Nurudeen"
    assert projected.initials == "IN"
    assert projected.source is team_inbox_read.InboxTimelineSenderSource.staff


def test_conversation_timeline_api_returns_404_for_inactive_conversation(db_session):
    conversation = InboxConversation(
        channel_type="email",
        status=InboxConversationStatus.open.value,
        is_active=False,
    )
    db_session.add(conversation)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        support_api.get_inbox_conversation_timeline(conversation.id, db=db_session)

    assert exc.value.status_code == 404
