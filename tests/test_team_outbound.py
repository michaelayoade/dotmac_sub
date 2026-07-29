from __future__ import annotations

from app.models.service_team import (
    ServiceTeam,
    ServiceTeamCapability,
    ServiceTeamCapabilityDefinition,
    ServiceTeamCapabilityKey,
)
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import email as email_service
from app.services import service_team_composition, team_outbound
from app.services.domain_settings import notification_settings


def _smtp_sender(db_session, key: str, *, host: str) -> None:
    email_service.upsert_smtp_sender(
        db_session,
        sender_key=key,
        host=host,
        port=587,
        username=f"{key}-user",
        password=f"{key}-pass",
        from_email=f"{key}@example.com",
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


def _team_with_capabilities(db_session, *keys: ServiceTeamCapabilityKey) -> ServiceTeam:
    team = ServiceTeam(name=f"Team {keys[0].value}", is_active=True)
    db_session.add(team)
    db_session.flush()
    for key in keys:
        contract = service_team_composition.CAPABILITY_CONTRACTS[key]
        if db_session.get(ServiceTeamCapabilityDefinition, key.value) is None:
            db_session.add(
                ServiceTeamCapabilityDefinition(
                    key=key.value,
                    display_name=contract.display_name,
                    contract_owner=contract.contract_owner,
                    contract_version=contract.contract_version,
                    description=f"Test definition for {key.value}",
                    is_active=True,
                )
            )
            db_session.flush()
        db_session.add(
            ServiceTeamCapability(
                team_id=team.id,
                capability_key=key.value,
                is_active=True,
            )
        )
    db_session.commit()
    return team


def test_support_capability_resolves_support_sender(db_session):
    _smtp_sender(db_session, "support", host="smtp.support.local")
    _activity_sender(db_session, "support_ticket", "support")
    team = _team_with_capabilities(
        db_session, ServiceTeamCapabilityKey.customer_support
    )

    resolved = team_outbound.resolve_team_email_sender(db_session, team=team)

    assert resolved.capability_keys == ("customer_support",)
    assert resolved.activity == "support_ticket"
    assert resolved.config["sender_key"] == "support"


def test_explicit_activity_handles_multi_capability_team(db_session):
    _smtp_sender(db_session, "field", host="smtp.field.local")
    _activity_sender(db_session, "field_service", "field")
    team = _team_with_capabilities(
        db_session,
        ServiceTeamCapabilityKey.customer_support,
        ServiceTeamCapabilityKey.field_service,
    )

    unresolved = team_outbound.resolve_team_email_sender(db_session, team=team)
    resolved = team_outbound.resolve_team_email_sender(
        db_session,
        team=team,
        fallback_activity="field_service",
    )

    assert unresolved.activity is None
    assert set(unresolved.capability_keys) == {"customer_support", "field_service"}
    assert resolved.activity == "field_service"
    assert resolved.config["sender_key"] == "field"


def test_route_sender_metadata_overrides_team_metadata(db_session):
    _smtp_sender(db_session, "team_support", host="smtp.team.local")
    _smtp_sender(db_session, "route_support", host="smtp.route.local")
    team = _team_with_capabilities(
        db_session, ServiceTeamCapabilityKey.customer_support
    )
    team.metadata_ = {"outbound_email_sender_key": "team_support"}
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
    assert resolved.config["host"] == "smtp.route.local"
