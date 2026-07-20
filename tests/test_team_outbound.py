from __future__ import annotations

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import email as email_service
from app.services import team_outbound
from app.services.domain_settings import notification_settings


def _smtp_sender(
    db_session,
    key: str,
    *,
    host: str,
    from_email: str | None = None,
) -> None:
    email_service.upsert_smtp_sender(
        db_session,
        sender_key=key,
        host=host,
        port=587,
        username=f"{key}-user",
        password=f"{key}-pass",
        from_email=from_email or f"{key}@example.com",
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


def test_support_team_resolves_support_ticket_sender(db_session):
    _smtp_sender(db_session, "support", host="smtp.support.local")
    _activity_sender(db_session, "support_ticket", "support")
    team = ServiceTeam(name="Support", team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.commit()

    resolved = team_outbound.resolve_team_email_sender(
        db_session, service_team_id=str(team.id)
    )

    assert resolved.service_team_id == str(team.id)
    assert resolved.team_type == ServiceTeamType.support.value
    assert resolved.sender_key is None
    assert resolved.activity == "support_ticket"
    assert resolved.config["sender_key"] == "support"
    assert resolved.config["host"] == "smtp.support.local"


def test_billing_team_resolves_billing_invoice_sender(db_session):
    _smtp_sender(db_session, "billing", host="smtp.billing.local")
    _activity_sender(db_session, "billing_invoice", "billing")
    team = ServiceTeam(name="Finance", team_type=ServiceTeamType.billing.value)
    db_session.add(team)
    db_session.commit()

    resolved = team_outbound.resolve_team_email_sender(db_session, team=team)

    assert resolved.activity == "billing_invoice"
    assert resolved.config["sender_key"] == "billing"
    assert resolved.config["from_email"] == "billing@example.com"


def test_field_service_team_resolves_work_order_sender(db_session):
    _smtp_sender(db_session, "field", host="smtp.field.local")
    _activity_sender(db_session, "field_service", "field")
    team = ServiceTeam(
        name="Field Service", team_type=ServiceTeamType.field_service.value
    )
    db_session.add(team)
    db_session.commit()

    resolved = team_outbound.resolve_team_email_sender(db_session, team=team)

    assert resolved.activity == "field_service"
    assert resolved.config["sender_key"] == "field"
    assert resolved.config["host"] == "smtp.field.local"


def test_team_metadata_sender_key_overrides_type_activity(db_session):
    _smtp_sender(db_session, "support", host="smtp.support.local")
    _smtp_sender(db_session, "vip_support", host="smtp.vip.local")
    _activity_sender(db_session, "support_ticket", "support")
    team = ServiceTeam(
        name="VIP Support",
        team_type=ServiceTeamType.support.value,
        metadata_={"outbound_email_sender_key": "VIP_Support"},
    )
    db_session.add(team)
    db_session.commit()

    resolved = team_outbound.resolve_team_email_sender(db_session, team=team)

    assert resolved.sender_key == "vip_support"
    assert resolved.activity == "support_ticket"
    assert resolved.config["sender_key"] == "vip_support"
    assert resolved.config["host"] == "smtp.vip.local"


def test_route_metadata_sender_key_overrides_team_metadata(db_session):
    _smtp_sender(db_session, "team_support", host="smtp.team.local")
    _smtp_sender(db_session, "route_support", host="smtp.route.local")
    team = ServiceTeam(
        name="Support",
        team_type=ServiceTeamType.support.value,
        metadata_={"outbound_email_sender_key": "team_support"},
    )
    db_session.add(team)
    db_session.commit()

    resolved = team_outbound.resolve_team_email_sender(
        db_session,
        team=team,
        metadata_override={
            team_outbound.OUTBOUND_EMAIL_SENDER_METADATA_KEY: "route_support",
            team_outbound.OUTBOUND_EMAIL_ACTIVITY_METADATA_KEY: "support_ticket",
        },
    )

    assert resolved.sender_key == "route_support"
    assert resolved.activity == "support_ticket"
    assert resolved.config["sender_key"] == "route_support"
    assert resolved.config["host"] == "smtp.route.local"


def test_unknown_team_uses_fallback_activity(db_session):
    _smtp_sender(db_session, "ops", host="smtp.ops.local")
    _activity_sender(db_session, "operations", "ops")

    resolved = team_outbound.resolve_team_email_sender(
        db_session,
        service_team_id="not-a-uuid",
        fallback_activity="operations",
    )

    assert resolved.service_team_id is None
    assert resolved.activity == "operations"
    assert resolved.config["sender_key"] == "ops"
