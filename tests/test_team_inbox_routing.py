from __future__ import annotations

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationTeam,
    InboxTeamRole,
    InboxTeamSource,
    TeamInboxEmailRoute,
)
from app.services import team_inbox_routing


def _team(db_session, name: str, team_type: str) -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=team_type)
    db_session.add(team)
    db_session.flush()
    return team


def _route(
    db_session,
    team: ServiceTeam,
    email: str,
    *,
    priority: int = 100,
    is_primary: bool = False,
) -> None:
    db_session.add(
        TeamInboxEmailRoute(
            service_team_id=team.id,
            email_address=email.lower(),
            priority=priority,
            is_primary=is_primary,
            is_active=True,
        )
    )
    db_session.flush()


def test_email_routing_collects_all_addressed_teams_without_duplicates(db_session):
    support = _team(db_session, "Support", ServiceTeamType.support.value)
    billing = _team(db_session, "Finance", ServiceTeamType.billing.value)
    field = _team(db_session, "Field Service", ServiceTeamType.field_service.value)
    _route(db_session, support, "support@dotmac.io", priority=10, is_primary=True)
    _route(db_session, billing, "billing@dotmac.io", priority=20)
    _route(db_session, field, "field@dotmac.io", priority=30)
    db_session.commit()

    plan = team_inbox_routing.build_email_team_routing_plan(
        db_session,
        to_addresses=["Support <support@dotmac.io>", "billing@dotmac.io"],
        cc_addresses=["FIELD@dotmac.io", "support@dotmac.io"],
    )

    assert plan.primary_service_team_id == str(support.id)
    assert plan.participant_service_team_ids == [
        str(support.id),
        str(billing.id),
        str(field.id),
    ]
    assert [match.email_address for match in plan.matches] == [
        "support@dotmac.io",
        "billing@dotmac.io",
        "field@dotmac.io",
    ]
    assert plan.unmatched_recipients == []


def test_email_routing_prefers_to_recipient_before_cc_recipient(db_session):
    support = _team(db_session, "Support", ServiceTeamType.support.value)
    billing = _team(db_session, "Finance", ServiceTeamType.billing.value)
    _route(db_session, support, "support@dotmac.io", priority=1, is_primary=True)
    _route(db_session, billing, "billing@dotmac.io", priority=99)
    db_session.commit()

    plan = team_inbox_routing.build_email_team_routing_plan(
        db_session,
        to_addresses=["billing@dotmac.io"],
        cc_addresses=["support@dotmac.io"],
    )

    assert plan.primary_service_team_id == str(billing.id)
    assert plan.participant_service_team_ids == [str(billing.id), str(support.id)]


def test_apply_routing_plan_sets_one_owner_and_participating_teams(db_session):
    support = _team(db_session, "Support", ServiceTeamType.support.value)
    billing = _team(db_session, "Finance", ServiceTeamType.billing.value)
    _route(db_session, support, "support@dotmac.io", priority=10)
    _route(db_session, billing, "billing@dotmac.io", priority=20)
    conversation = InboxConversation(
        channel_type="email",
        subject="Need help with my invoice and install",
        contact_address="customer@example.com",
    )
    db_session.add(conversation)
    db_session.commit()

    plan = team_inbox_routing.build_email_team_routing_plan(
        db_session,
        to_addresses=["support@dotmac.io", "billing@dotmac.io"],
    )
    team_inbox_routing.apply_email_routing_plan(
        db_session, conversation=conversation, plan=plan
    )
    db_session.commit()

    links = (
        db_session.query(InboxConversationTeam)
        .filter(InboxConversationTeam.conversation_id == conversation.id)
        .order_by(InboxConversationTeam.role.desc())
        .all()
    )

    assert conversation.primary_service_team_id == support.id
    assert {str(link.service_team_id) for link in links} == {
        str(support.id),
        str(billing.id),
    }
    assert [
        (str(link.service_team_id), link.role, link.source) for link in links
    ] == [
        (
            str(billing.id),
            InboxTeamRole.participant.value,
            InboxTeamSource.recipient_to.value,
        ),
        (
            str(support.id),
            InboxTeamRole.owner.value,
            InboxTeamSource.recipient_to.value,
        ),
    ]


def test_email_routing_uses_fallback_team_when_no_address_matches(db_session):
    support = _team(db_session, "Support", ServiceTeamType.support.value)
    db_session.commit()

    plan = team_inbox_routing.build_email_team_routing_plan(
        db_session,
        to_addresses=["unknown@dotmac.io"],
        fallback_service_team_id=support.id,
    )

    assert plan.primary_service_team_id == str(support.id)
    assert plan.participant_service_team_ids == [str(support.id)]
    assert plan.matches == []
    assert plan.unmatched_recipients == ["unknown@dotmac.io"]
