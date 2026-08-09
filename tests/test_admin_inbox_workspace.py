from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from app.models.organization import Organization
from app.models.party import PartyRelationship, PartyRole, PartyRoleType
from app.models.sales import Lead, LeadOriginCapture
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceStatus,
    InboxConversation,
    InboxConversationLabel,
    InboxConversationStatus,
    InboxLabel,
    InboxMessage,
    InboxSavedFilter,
)
from app.services import (
    team_inbox_commands,
    team_inbox_outbound,
    team_inbox_projection,
    team_inbox_read,
)


def _conversation(db_session) -> uuid.UUID:
    conversation = InboxConversation(
        channel_type="email",
        subject="Router offline",
        contact_address="customer@example.com",
        status=InboxConversationStatus.open.value,
    )
    db_session.add(conversation)
    db_session.flush()
    conversation_id = conversation.id
    db_session.commit()
    return conversation_id


def test_inbox_workspace_templates_compile():
    environment = Environment(loader=FileSystemLoader("templates"), autoescape=True)

    for template_name in (
        "admin/inbox/index.html",
        "admin/inbox/_sidebar.html",
        "admin/inbox/_conversation.html",
        "admin/inbox/_contact_drawer.html",
        "admin/inbox/_empty_state.html",
        "admin/inbox/_overlays.html",
        "admin/inbox/manager_ai.html",
    ):
        assert environment.get_template(template_name) is not None


def test_workspace_exposes_responsive_realtime_and_accessible_controls():
    index = Path("templates/admin/inbox/index.html").read_text()
    sidebar = Path("templates/admin/inbox/_sidebar.html").read_text()
    conversation = Path("templates/admin/inbox/_conversation.html").read_text()
    javascript = Path("static/js/admin-inbox.js").read_text()

    assert "startSidebarResize" in index
    assert "inbox-sidebar-content" in index
    assert 'role="dialog"' in Path("templates/admin/inbox/_overlays.html").read_text()
    assert "@input.debounce.300ms" in sidebar
    assert "/admin/inbox/presence" in sidebar
    assert "/admin/inbox/manager-ai" in sidebar
    assert "support:inbox_ai:read" in sidebar
    assert "Only online agents receive auto-assigned inbox conversations." in sidebar
    assert "conversation_id" in sidebar
    assert "import message_bubble with context" in conversation
    assert "data-reply-composer" in conversation
    assert "idempotency_key" in conversation
    triage = Path("templates/components/ui/triage.html").read_text()
    assert 'set priority_label = "Urgent"' in triage
    assert "assignee.initials" in triage
    assert "message.author_display_name" in triage
    assert 'title="{{ author_name }}"' in triage
    assert "dotmac.inbox.draft." in javascript
    assert "newMessagesAvailable" in javascript
    assert "setInterval" in javascript
    assert "5000" in javascript
    assert "handleShortcut" in javascript


def test_projection_supplies_live_agent_and_assignment_options(db_session):
    user = SystemUser(
        first_name="Ada",
        last_name="Agent",
        display_name="Ada Agent",
        email="ada-agent@example.test",
    )
    team = ServiceTeam(name="Support", team_type=ServiceTeamType.support.value)
    db_session.add_all([user, team])
    db_session.flush()
    db_session.add(ServiceTeamMember(team_id=team.id, person_id=user.id))
    conversation = InboxConversation(
        channel_type="email",
        subject="Help",
        contact_address="customer@example.test",
    )
    db_session.add(conversation)
    db_session.commit()

    projection = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(actor_person_id=user.id),
    )

    assert projection.agent_options[0].name == "Ada Agent"
    assert projection.agent_options[0].initials == "AA"
    assert projection.agent_presence is not None
    assert projection.agent_presence.status == InboxAgentPresenceStatus.offline.value
    assert projection.assignment_counts.all == 1
    assert projection.assignment_counts.unassigned == 1


def test_projection_reads_current_agent_presence(db_session):
    user = SystemUser(
        first_name="Ada",
        last_name="Agent",
        display_name="Ada Agent",
        email="ada-agent-presence@example.test",
    )
    team = ServiceTeam(name="Support", team_type=ServiceTeamType.support.value)
    db_session.add_all([user, team])
    db_session.flush()
    db_session.add(ServiceTeamMember(team_id=team.id, person_id=user.id))
    db_session.add(
        InboxAgentPresence(
            person_id=user.id,
            status=InboxAgentPresenceStatus.online.value,
            manual_override_status=InboxAgentPresenceStatus.away.value,
        )
    )
    db_session.commit()

    projection = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(actor_person_id=user.id),
    )

    assert projection.agent_presence is not None
    assert projection.agent_presence.status == InboxAgentPresenceStatus.away.value


def test_set_agent_presence_command_updates_current_operator(db_session):
    person_id = uuid.uuid4()

    outcome = team_inbox_commands.set_agent_presence(
        db_session,
        actor_person_id=person_id,
        status=InboxAgentPresenceStatus.online.value,
    )

    presence = (
        db_session.query(InboxAgentPresence)
        .filter(InboxAgentPresence.person_id == person_id)
        .one()
    )
    assert outcome.status == InboxAgentPresenceStatus.online.value
    assert outcome.already_set is False
    assert presence.manual_override_status == InboxAgentPresenceStatus.online.value
    assert presence.last_seen_at is not None


def test_projection_keeps_direct_conversation_link_when_page_is_canonicalized(
    db_session,
):
    conversation_id = _conversation(db_session)

    projection = team_inbox_projection.build_queue_projection(
        db_session,
        team_inbox_projection.InboxQueueRequest(
            page=99,
            selected_conversation_id=conversation_id,
        ),
    )

    assert projection.canonical_url is not None
    assert f"conversation_id={conversation_id}" in projection.canonical_url


def test_conversation_row_projects_labels_and_send_failure_summary(db_session):
    conversation_id = _conversation(db_session)
    label = InboxLabel(name="VIP", slug=f"vip-{uuid.uuid4()}")
    db_session.add(label)
    db_session.flush()
    db_session.add(
        InboxConversationLabel(
            conversation_id=conversation_id,
            label_id=label.id,
        )
    )
    db_session.add(
        InboxMessage(
            conversation_id=conversation_id,
            channel_type="email",
            direction="outbound",
            body="Delivery attempt",
            metadata_={
                "delivery_status": "failed",
                "send_error": "Recipient server rejected the message",
            },
        )
    )
    db_session.commit()

    row = team_inbox_read.list_conversations(db_session).items[0]

    assert [item.name for item in row.labels] == ["VIP"]
    assert row.latest_delivery_status == "failed"
    assert row.latest_delivery_error == "Recipient server rejected the message"


def test_reply_idempotency_key_replays_without_duplicate_message(
    db_session,
    monkeypatch,
):
    conversation_id = _conversation(db_session)
    calls = 0

    def fake_send(db, *, conversation, payload, record_failure):
        nonlocal calls
        calls += 1
        message = InboxMessage(
            conversation_id=conversation.id,
            channel_type="email",
            direction="outbound",
            body=payload.body_text,
            from_address="support@example.test",
            to_addresses=[conversation.contact_address],
            cc_addresses=[],
            metadata_={
                **dict(payload.metadata or {}),
                "body_text": payload.body_text,
                "delivery_status": "queued",
            },
        )
        db.add(message)
        db.flush()
        return team_inbox_outbound.InboxReplyResult(
            kind="queued",
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            from_address=message.from_address,
        )

    monkeypatch.setattr(
        team_inbox_commands.team_inbox_outbound,
        "send_inbox_reply",
        fake_send,
    )

    first = team_inbox_commands.reply(
        db_session,
        conversation_id=conversation_id,
        body_text="We are checking.",
        actor_person_id=uuid.uuid4(),
        idempotency_key="send-key-1",
    )
    second = team_inbox_commands.reply(
        db_session,
        conversation_id=conversation_id,
        body_text="We are checking.",
        actor_person_id=uuid.uuid4(),
        idempotency_key="send-key-1",
    )

    assert first.replayed is False
    assert second.replayed is True
    assert calls == 1
    assert db_session.query(InboxMessage).count() == 1


def test_reply_idempotency_key_rejects_changed_body(db_session, monkeypatch):
    conversation_id = _conversation(db_session)

    def fake_send(db, *, conversation, payload, record_failure):
        message = InboxMessage(
            conversation_id=conversation.id,
            channel_type="email",
            direction="outbound",
            body=payload.body_text,
            from_address="support@example.test",
            to_addresses=[conversation.contact_address],
            cc_addresses=[],
            metadata_={
                **dict(payload.metadata or {}),
                "body_text": payload.body_text,
                "delivery_status": "queued",
            },
        )
        db.add(message)
        db.flush()
        return team_inbox_outbound.InboxReplyResult(
            kind="queued",
            conversation_id=str(conversation.id),
            message_id=str(message.id),
        )

    monkeypatch.setattr(
        team_inbox_commands.team_inbox_outbound,
        "send_inbox_reply",
        fake_send,
    )
    team_inbox_commands.reply(
        db_session,
        conversation_id=conversation_id,
        body_text="Original",
        actor_person_id=uuid.uuid4(),
        idempotency_key="send-key-2",
    )

    with pytest.raises(
        team_inbox_commands.InboxCommandRejected,
        match="different reply",
    ):
        team_inbox_commands.reply(
            db_session,
            conversation_id=conversation_id,
            body_text="Changed",
            actor_person_id=uuid.uuid4(),
            idempotency_key="send-key-2",
        )


def test_only_saved_view_owner_can_delete(db_session):
    owner_id = uuid.uuid4()
    saved_filter = InboxSavedFilter(
        name="My queue",
        owner_person_id=owner_id,
        filter_payload={"open_only": True},
    )
    db_session.add(saved_filter)
    db_session.flush()
    saved_filter_id = saved_filter.id
    db_session.commit()

    with pytest.raises(team_inbox_commands.InboxCommandRejected):
        team_inbox_commands.delete_filter(
            db_session,
            filter_id=saved_filter_id,
            actor_person_id=uuid.uuid4(),
        )

    team_inbox_commands.delete_filter(
        db_session,
        filter_id=saved_filter_id,
        actor_person_id=owner_id,
    )
    assert db_session.get(InboxSavedFilter, saved_filter_id).is_active is False


def test_bulk_priority_action_uses_existing_command_owner(db_session):
    conversation_id = _conversation(db_session)

    outcome = team_inbox_commands.bulk_action(
        db_session,
        conversation_ids=[conversation_id],
        action="priority",
        priority=25,
        actor_person_id=uuid.uuid4(),
    )

    conversation = db_session.get(InboxConversation, conversation_id)
    assert outcome.message == "Updated priority for 1 conversations."
    assert conversation.priority == 25
    assert conversation.metadata_["priority_history"][0]["to"] == 25


def test_create_lead_from_unmatched_conversation_is_idempotent(db_session):
    conversation_id = _conversation(db_session)

    first = team_inbox_commands.create_lead_from_conversation(
        db_session,
        conversation_id=conversation_id,
        actor_person_id=uuid.uuid4(),
    )
    second = team_inbox_commands.create_lead_from_conversation(
        db_session,
        conversation_id=conversation_id,
        actor_person_id=uuid.uuid4(),
    )

    conversation = db_session.get(InboxConversation, conversation_id)
    lead = db_session.get(Lead, first.lead_id)
    origin = db_session.query(LeadOriginCapture).one()
    assert second.replayed is True
    assert second.lead_id == first.lead_id
    assert lead is not None
    assert lead.party_id is not None
    assert lead.subscriber_id is None
    assert origin.source_interaction_id == f"team-inbox:{conversation_id}"
    assert conversation.metadata_["lead_capture"]["lead_id"] == first.lead_id
    assert db_session.query(Lead).count() == 1


def test_merge_conversation_lead_to_customer_attaches_account(db_session):
    conversation_id = _conversation(db_session)
    lead = team_inbox_commands.create_lead_from_conversation(
        db_session,
        conversation_id=conversation_id,
        actor_person_id=uuid.uuid4(),
    )
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Customer",
        email="ada-merge@example.test",
        status=SubscriberStatus.active,
        is_active=True,
    )
    db_session.add(subscriber)
    db_session.flush()
    subscriber_id = subscriber.id
    db_session.commit()

    outcome = team_inbox_commands.merge_conversation_lead(
        db_session,
        conversation_id=conversation_id,
        target_type="subscriber",
        subscriber_id=subscriber_id,
        actor_person_id=uuid.uuid4(),
    )

    lead_row = db_session.get(Lead, lead.lead_id)
    conversation = db_session.get(InboxConversation, conversation_id)
    subscriber = db_session.get(Subscriber, subscriber_id)
    assert outcome.subscriber_id == str(subscriber_id)
    assert lead_row.subscriber_id == subscriber_id
    assert subscriber.party_id == lead_row.party_id
    assert conversation.subscriber_id == subscriber_id
    assert (
        conversation.metadata_["lead_capture"]["merge"]["target_type"] == "subscriber"
    )


def test_merge_conversation_lead_to_reseller_records_party_profile(db_session):
    conversation_id = _conversation(db_session)
    lead = team_inbox_commands.create_lead_from_conversation(
        db_session,
        conversation_id=conversation_id,
        actor_person_id=uuid.uuid4(),
    )
    reseller = Reseller(name="North Channel", code="north-channel", is_active=True)
    db_session.add(reseller)
    db_session.flush()
    reseller_id = reseller.id
    db_session.commit()

    outcome = team_inbox_commands.merge_conversation_lead(
        db_session,
        conversation_id=conversation_id,
        target_type="reseller",
        reseller_id=reseller_id,
        actor_person_id=uuid.uuid4(),
    )

    lead_row = db_session.get(Lead, lead.lead_id)
    conversation = db_session.get(InboxConversation, conversation_id)
    reseller = db_session.get(Reseller, reseller_id)
    role = (
        db_session.query(PartyRole)
        .filter(
            PartyRole.party_id == reseller.party_id,
            PartyRole.role_type == PartyRoleType.reseller.value,
        )
        .one()
    )
    relationship = (
        db_session.query(PartyRelationship)
        .filter(
            PartyRelationship.subject_party_id == lead_row.party_id,
            PartyRelationship.object_party_id == reseller.party_id,
        )
        .one()
    )
    assert outcome.reseller_id == str(reseller_id)
    assert reseller.party_id is not None
    assert role.status == "active"
    assert relationship.relationship_type == "contact_for"
    assert conversation.metadata_["lead_capture"]["merge"]["target_type"] == "reseller"


def test_merge_conversation_lead_to_business_records_organization_profile(db_session):
    conversation_id = _conversation(db_session)
    lead = team_inbox_commands.create_lead_from_conversation(
        db_session,
        conversation_id=conversation_id,
        actor_person_id=uuid.uuid4(),
    )
    organization = Organization(name="Metro Fiber Ltd", is_active=True)
    db_session.add(organization)
    db_session.flush()
    organization_id = organization.id
    db_session.commit()

    outcome = team_inbox_commands.merge_conversation_lead(
        db_session,
        conversation_id=conversation_id,
        target_type="organization",
        organization_id=organization_id,
        actor_person_id=uuid.uuid4(),
    )

    lead_row = db_session.get(Lead, lead.lead_id)
    organization = db_session.get(Organization, organization_id)
    relationship = (
        db_session.query(PartyRelationship)
        .filter(
            PartyRelationship.subject_party_id == lead_row.party_id,
            PartyRelationship.object_party_id == organization.party_id,
        )
        .one()
    )
    assert outcome.organization_id == str(organization_id)
    assert organization.party_id is not None
    assert relationship.relationship_type == "contact_for"
    assert lead_row.metadata_["inbox_merge"]["target_type"] == "organization"
