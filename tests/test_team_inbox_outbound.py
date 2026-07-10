from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import support as support_api
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.subscription_engine import SettingValueType
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
    InboxTeamRole,
)
from app.schemas.settings import DomainSettingUpdate
from app.schemas.team_inbox import InboxConversationReplyRequest
from app.services import email as email_service
from app.services import team_inbox_outbound, team_outbound
from app.services.domain_settings import notification_settings


def _smtp_sender(db_session, key: str, *, from_email: str) -> None:
    email_service.upsert_smtp_sender(
        db_session,
        sender_key=key,
        host=f"smtp.{key}.local",
        port=587,
        username=f"{key}-user",
        password=f"{key}-pass",
        from_email=from_email,
        from_name=key.title(),
        use_tls=True,
        use_ssl=False,
        is_active=True,
    )


def _activity_sender(db_session, activity: str, sender_key: str) -> None:
    notification_settings.upsert_by_key(
        db_session,
        f"smtp_activity_sender.{activity}",
        DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text=sender_key,
        ),
    )


def _team(db_session, name: str, team_type: str) -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=team_type)
    db_session.add(team)
    db_session.flush()
    return team


def _conversation(db_session, team: ServiceTeam) -> InboxConversation:
    conversation = InboxConversation(
        channel_type="email",
        subject="Router offline",
        contact_address="customer@example.com",
        primary_service_team_id=team.id,
        status=InboxConversationStatus.open.value,
        first_message_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        last_message_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxConversationTeam(
            conversation_id=conversation.id,
            service_team_id=team.id,
            role=InboxTeamRole.owner.value,
            is_active=True,
        )
    )
    db_session.flush()
    return conversation


def test_send_inbox_reply_uses_owner_team_sender(db_session, monkeypatch):
    _smtp_sender(db_session, "support", from_email="support@dotmac.io")
    _activity_sender(db_session, "support_ticket", "support")
    team = _team(db_session, "Support", ServiceTeamType.support.value)
    conversation = _conversation(db_session, team)
    sent: dict[str, object] = {}

    def _fake_send_email(*args, **kwargs):
        sent.update(kwargs)
        return True

    monkeypatch.setattr(
        team_inbox_outbound.email_service, "send_email", _fake_send_email
    )
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>We are checking.</p>",
            body_text="We are checking.",
            sent_by_person_id=uuid4(),
        ),
        now=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
    )
    db_session.commit()

    message = db_session.query(InboxMessage).one()
    assert result.kind == "sent"
    assert result.sender_key == "support"
    assert result.activity == "support_ticket"
    assert result.from_address == "support@dotmac.io"
    assert sent["to_email"] == "customer@example.com"
    assert sent["subject"] == "Re: Router offline"
    assert sent["activity"] == "support_ticket"
    assert message.direction == InboxMessageDirection.outbound.value
    assert message.from_address == "support@dotmac.io"
    assert message.to_addresses == ["customer@example.com"]
    assert message.metadata_["sender_key"] == "support"


def test_send_inbox_reply_uses_field_service_sender_for_field_team(
    db_session, monkeypatch
):
    _smtp_sender(db_session, "field", from_email="field@dotmac.io")
    _activity_sender(db_session, "field_service", "field")
    team = _team(db_session, "Field Service", ServiceTeamType.field_service.value)
    conversation = _conversation(db_session, team)
    sent: dict[str, object] = {}
    monkeypatch.setattr(
        team_inbox_outbound.email_service,
        "send_email",
        lambda *args, **kwargs: sent.update(kwargs) or True,
    )
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html="<p>Technician is on route.</p>",
            to_email="site-contact@example.com",
        ),
    )

    assert result.kind == "sent"
    assert result.activity == "field_service"
    assert result.from_address == "field@dotmac.io"
    assert sent["to_email"] == "site-contact@example.com"
    assert sent["activity"] == "field_service"


def test_reply_api_returns_502_and_does_not_store_message_on_send_failure(
    db_session, monkeypatch
):
    team = _team(db_session, "Support", ServiceTeamType.support.value)
    conversation = _conversation(db_session, team)
    monkeypatch.setattr(
        team_inbox_outbound.email_service,
        "send_email",
        lambda *_, **__: False,
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        support_api.reply_to_inbox_conversation(
            conversation.id,
            InboxConversationReplyRequest(body_html="<p>No route.</p>"),
            auth={"principal_id": str(uuid4())},
            db=db_session,
        )

    assert exc.value.status_code == 502
    assert db_session.query(InboxMessage).count() == 0


def test_team_metadata_sender_key_overrides_reply_activity(db_session, monkeypatch):
    _smtp_sender(db_session, "vip_support", from_email="vip@dotmac.io")
    team = ServiceTeam(
        name="VIP Support",
        team_type=ServiceTeamType.support.value,
        metadata_={
            team_outbound.OUTBOUND_EMAIL_SENDER_METADATA_KEY: "vip_support",
        },
    )
    db_session.add(team)
    db_session.flush()
    conversation = _conversation(db_session, team)
    sent: dict[str, object] = {}
    monkeypatch.setattr(
        team_inbox_outbound.email_service,
        "send_email",
        lambda *args, **kwargs: sent.update(kwargs) or True,
    )
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(body_html="<p>VIP reply.</p>"),
    )

    assert result.kind == "sent"
    assert result.sender_key == "vip_support"
    assert result.from_address == "vip@dotmac.io"
    assert sent["sender_key"] == "vip_support"


def test_owner_route_sender_metadata_overrides_team_sender(db_session, monkeypatch):
    _smtp_sender(db_session, "team_support", from_email="support@dotmac.io")
    _smtp_sender(db_session, "route_support", from_email="help@dotmac.io")
    team = ServiceTeam(
        name="Support",
        team_type=ServiceTeamType.support.value,
        metadata_={
            team_outbound.OUTBOUND_EMAIL_SENDER_METADATA_KEY: "team_support",
        },
    )
    db_session.add(team)
    db_session.flush()
    conversation = InboxConversation(
        channel_type="email",
        subject="Need help",
        contact_address="customer@example.com",
        primary_service_team_id=team.id,
        status=InboxConversationStatus.open.value,
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxConversationTeam(
            conversation_id=conversation.id,
            service_team_id=team.id,
            role=InboxTeamRole.owner.value,
            is_active=True,
            metadata_={
                team_outbound.OUTBOUND_EMAIL_SENDER_METADATA_KEY: "route_support",
                team_outbound.OUTBOUND_EMAIL_ACTIVITY_METADATA_KEY: "support_ticket",
                "route_email_address": "help@dotmac.io",
            },
        )
    )
    sent: dict[str, object] = {}
    monkeypatch.setattr(
        team_inbox_outbound.email_service,
        "send_email",
        lambda *args, **kwargs: sent.update(kwargs) or True,
    )
    db_session.commit()

    result = team_inbox_outbound.send_inbox_reply(
        db_session,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(body_html="<p>Reply.</p>"),
    )

    assert result.kind == "sent"
    assert result.sender_key == "route_support"
    assert result.activity == "support_ticket"
    assert result.from_address == "help@dotmac.io"
    assert sent["sender_key"] == "route_support"
