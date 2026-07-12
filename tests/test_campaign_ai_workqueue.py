from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.ai_insight import AIInsightStatus
from app.models.comms_campaign import CampaignRecipient, CampaignRecipientStatus
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamMember,
    ServiceTeamMemberRole,
    ServiceTeamType,
)
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.models.support import Ticket
from app.models.team_inbox import InboxConversation, InboxMessage
from app.models.workqueue import WorkqueueSnooze
from app.schemas.ai_operations import AIInsightCreate, AiIntakeConfigUpsert
from app.schemas.campaigns import CampaignCreate
from app.services import ai_operations, comms_campaigns, workqueue


def _subscriber(
    db_session,
    *,
    email: str,
    phone: str | None = "08035550114",
    status: SubscriberStatus = SubscriberStatus.active,
    is_active: bool = True,
    reseller: Reseller | None = None,
) -> Subscriber:
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Nwosu",
        email=email,
        phone=phone,
        status=status,
        is_active=is_active,
        reseller=reseller,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _team(db_session, name: str = "Support") -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def test_campaign_build_skips_inactive_and_sends_through_native_inbox(
    db_session,
    monkeypatch,
):
    team = _team(db_session)
    active = _subscriber(db_session, email="ada@example.com")
    _subscriber(
        db_session,
        email="disabled@example.com",
        status=SubscriberStatus.disabled,
    )
    _subscriber(
        db_session,
        email="canceled@example.com",
        status=SubscriberStatus.canceled,
    )
    inactive_reseller = Reseller(name="Inactive Partner", is_active=False)
    db_session.add(inactive_reseller)
    db_session.flush()
    _subscriber(
        db_session,
        email="partner@example.com",
        reseller=inactive_reseller,
    )
    sent: list[dict] = []

    def _fake_send_email(*args, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        comms_campaigns.team_inbox_outbound.email_service,
        "send_email",
        _fake_send_email,
    )

    campaign = comms_campaigns.create_campaign(
        db_session,
        CampaignCreate(
            name="Outage Update",
            channel="email",
            subject="Service update",
            body_html="<p>We are on it.</p>",
            body_text="We are on it.",
            service_team_id=team.id,
        ),
    )

    audience = comms_campaigns.build_recipient_list(db_session, campaign.id)
    result = comms_campaigns.send_campaign_batch(db_session, campaign.id)

    recipients = db_session.query(CampaignRecipient).all()
    message = db_session.query(InboxMessage).one()
    conversation = db_session.query(InboxConversation).one()
    assert audience.created == 1
    assert audience.total_recipients == 1
    assert result.sent == 1
    assert recipients[0].subscriber_id == active.id
    assert recipients[0].status == CampaignRecipientStatus.sent.value
    assert conversation.subscriber_id == active.id
    assert conversation.primary_service_team_id == team.id
    assert message.metadata_["source_route"] == "native_campaign"
    assert sent[0]["to_email"] == "ada@example.com"


def test_ai_insight_acknowledge_expire_and_intake_config(db_session):
    insight = ai_operations.create_insight(
        db_session,
        AIInsightCreate(
            persona_key="campaign_optimizer",
            domain="campaigns",
            entity_type="campaign",
            entity_id=str(uuid.uuid4()),
            title="High failure rate",
            summary="A campaign has too many failed recipients.",
            expires_at=datetime.now(UTC) - timedelta(minutes=5),
        ),
    )

    acknowledged = ai_operations.acknowledge_insight(db_session, insight.id)
    stale = ai_operations.create_insight(
        db_session,
        AIInsightCreate(
            persona_key="campaign_optimizer",
            domain="campaigns",
            entity_type="campaign",
            entity_id=str(uuid.uuid4()),
            title="Stale draft insight",
            summary="This unresolved insight should expire.",
            expires_at=datetime.now(UTC) - timedelta(minutes=5),
        ),
    )
    expired = ai_operations.expire_stale_insights(db_session)
    config = ai_operations.upsert_intake_config(
        db_session,
        AiIntakeConfigUpsert(
            scope_key="inbox:default",
            channel_type="email",
            is_enabled=True,
            fallback_team_id=uuid.uuid4(),
            department_mappings=[{"keyword": "billing", "team": "finance"}],
        ),
    )

    assert acknowledged.status == AIInsightStatus.acknowledged.value
    assert expired == 1
    assert insight.status == AIInsightStatus.acknowledged.value
    assert stale.status == AIInsightStatus.expired.value
    assert config.scope_key == "inbox:default"
    assert config.is_enabled is True


def test_workqueue_aggregates_native_items_and_respects_snooze(db_session):
    user_id = uuid.uuid4()
    team = _team(db_session)
    subscriber = _subscriber(db_session, email="queue@example.com")
    conversation = InboxConversation(
        subscriber_id=subscriber.id,
        primary_service_team_id=team.id,
        channel_type="email",
        subject="Router offline",
        contact_address="queue@example.com",
        status="open",
        priority=20,
        last_message_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
    )
    ticket = Ticket(
        subscriber_id=subscriber.id,
        assigned_to_person_id=user_id,
        service_team_id=team.id,
        title="Packet loss",
        status="open",
        priority="urgent",
    )
    db_session.add_all([conversation, ticket])
    db_session.flush()

    # list_workqueue() now scopes reads to what the principal may actually see;
    # the old signature took a bare user_id and applied no authorization at all.
    # This user is an ordinary member of the team, which is what the original
    # test implied: they see their own assigned ticket plus the team's
    # unassigned conversation.
    db_session.add(
        ServiceTeamMember(
            team_id=team.id,
            person_id=user_id,
            role=ServiceTeamMemberRole.member.value,
        )
    )
    db_session.flush()
    principal = workqueue.WorkqueuePrincipal(
        person_id=user_id,
        roles=frozenset(),
        scopes=frozenset(),
        can_view=True,
        can_act=True,
    )

    items = workqueue.list_workqueue(
        db_session,
        principal,
        service_team_id=team.id,
    )
    workqueue.snooze_item(
        db_session,
        user_id=user_id,
        item_kind="ticket",
        item_id=ticket.id,
        snooze_until=datetime.now(UTC) + timedelta(hours=1),
    )
    unsnoozed = workqueue.list_workqueue(
        db_session,
        principal,
        service_team_id=team.id,
    )

    assert {item.item_kind for item in items} >= {"conversation", "ticket"}
    assert all(item.item_id != ticket.id for item in unsnoozed)
    assert db_session.query(WorkqueueSnooze).count() == 1
