from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.models.integration_platform import IntegrationInstallation
from app.models.party import PartyType
from app.models.sales import Lead, LeadOriginCapture
from app.services import party as party_service
from app.services.events.types import Event, EventType
from app.services.integrations import installations, meta_lead_conversion
from app.services.integrations.meta_social_contracts import MetaLeadConversionOutcome
from app.services.integrations.meta_social_installation import (
    META_SOCIAL_CONFIGURATION_SCOPE,
    ConfigureMetaSocialInstallationCommand,
    configure_meta_social_installation,
)
from app.services.integrations.runtime import ValidationResult
from app.services.owner_commands import CommandContext


def _configure_conversion(db_session) -> IntegrationInstallation:
    result = configure_meta_social_installation(
        db_session,
        ConfigureMetaSocialInstallationCommand(
            auth_mode="individual",
            app_id="app-1",
            facebook_page_id="page-1",
            instagram_account_id="ig-1",
            graph_version="v21.0",
            webhook_url="https://sub.example.test/api/v1/webhooks/meta",
            meta_oauth_access_token_ref="",
            facebook_page_access_token_ref=(
                "bao://secret/integrations/meta_social#facebook_page_access_token"
            ),
            instagram_login_access_token_ref=(
                "bao://secret/integrations/meta_social#instagram_login_access_token"
            ),
            webhook_signing_secret_ref=(
                "bao://secret/integrations/meta_social#webhook_signing_secret"
            ),
            webhook_verify_token_ref=(
                "bao://secret/integrations/meta_social#webhook_verify_token"
            ),
            conversion_dataset_id="dataset-1",
            conversion_event_name="CustomerConverted",
            conversions_api_access_token_ref=(
                "bao://secret/integrations/meta_social#conversions_api_access_token"
            ),
            environment="test",
        ),
        context=CommandContext.system(
            actor="test",
            scope=META_SOCIAL_CONFIGURATION_SCOPE,
            reason="Configure Meta conversion test",
            idempotency_key="test-meta-conversion-config",
        ),
    )
    installation = db_session.get(IntegrationInstallation, result.installation_id)
    assert installation is not None
    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
    )
    db_session.commit()
    return installation


def test_account_conversion_stages_one_meta_delivery_without_contact_data(db_session):
    _configure_conversion(db_session)
    party = party_service.create_party(
        db_session, party_type=PartyType.person, display_name="Meta Prospect"
    )
    lead = Lead(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="sales.capture",
        party_binding_reason="Verified Meta capture",
        title="Meta Lead",
        status="won",
        lead_source="Facebook Ads",
        is_active=True,
    )
    db_session.add(lead)
    db_session.flush()
    db_session.add(
        LeadOriginCapture(
            lead_id=lead.id,
            source_interaction_id="leadgen-42",
            capture_fingerprint="a" * 64,
            capture_method="ad_lead_form_webhook",
            source_platform="meta",
            lead_source="Facebook Ads",
            external_campaign_id="campaign-1",
            external_form_id="form-1",
            captured_at=datetime.now(UTC),
            capture_source="meta.lead_ads_webhook",
            capture_reason="Verified Meta Lead",
        )
    )
    db_session.commit()
    event = Event(
        event_type=EventType.lead_account_converted,
        event_id=uuid4(),
        occurred_at=datetime.now(UTC),
        payload={
            "lead_id": str(lead.id),
            "subscriber_id": str(uuid4()),
            "outcome": "created",
        },
    )

    first = meta_lead_conversion.stage_conversion_for_event(db_session, event=event)
    second = meta_lead_conversion.stage_conversion_for_event(db_session, event=event)

    assert first is not None
    assert second is not None
    assert first.id == second.id
    assert first.payload_json == {
        "lead_id": str(lead.id),
        "leadgen_id": "leadgen-42",
        "converted_at": event.occurred_at.isoformat(),
        "event_id": str(event.event_id),
    }
    assert "email" not in first.payload_json
    assert "phone" not in first.payload_json


def test_meta_conversion_delivery_records_provider_acceptance(db_session, monkeypatch):
    _configure_conversion(db_session)
    party = party_service.create_party(
        db_session, party_type=PartyType.person, display_name="Meta Prospect"
    )
    lead = Lead(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="sales.capture",
        party_binding_reason="Verified Meta capture",
        title="Meta Lead",
        status="won",
        lead_source="Facebook Ads",
        is_active=True,
    )
    db_session.add(lead)
    db_session.flush()
    db_session.add(
        LeadOriginCapture(
            lead_id=lead.id,
            source_interaction_id="leadgen-accepted",
            capture_fingerprint="b" * 64,
            capture_method="ad_lead_form_webhook",
            source_platform="meta",
            lead_source="Facebook Ads",
            external_campaign_id="campaign-1",
            external_form_id="form-1",
            captured_at=datetime.now(UTC),
            capture_source="meta.lead_ads_webhook",
            capture_reason="Verified Meta Lead",
        )
    )
    db_session.commit()
    event = Event(
        event_type=EventType.lead_account_converted,
        event_id=uuid4(),
        occurred_at=datetime.now(UTC),
        payload={"lead_id": str(lead.id)},
    )
    delivery = meta_lead_conversion.stage_conversion_for_event(db_session, event=event)
    assert delivery is not None
    delivery_id = delivery.id
    db_session.commit()
    sent: list[str] = []

    def accept(db, command):
        sent.append(command.event_id)
        return MetaLeadConversionOutcome(
            accepted=True,
            operation_status="succeeded",
            error_code=None,
        )

    monkeypatch.setattr(meta_lead_conversion, "send_lead_conversion", accept)
    result = meta_lead_conversion.deliver_conversion(
        db_session,
        meta_lead_conversion.DeliverMetaLeadConversionCommand(
            context=CommandContext.system(
                actor="test",
                scope=meta_lead_conversion.META_LEAD_CONVERSION_DELIVERY_SCOPE,
                reason="Test Meta conversion delivery",
                idempotency_key=f"test-meta-delivery:{delivery_id}",
            ),
            delivery_id=delivery_id,
        ),
    )

    assert result.state == "delivered"
    assert sent == [str(event.event_id)]


def test_exact_existing_customer_match_stages_one_conversion(db_session):
    _configure_conversion(db_session)
    party = party_service.create_party(
        db_session, party_type=PartyType.person, display_name="Existing Customer Lead"
    )
    lead = Lead(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="sales.capture",
        party_binding_reason="Verified Meta capture",
        title="Meta Lead",
        status="new",
        lead_source="Facebook Ads",
        is_active=True,
    )
    db_session.add(lead)
    db_session.flush()
    db_session.add(
        LeadOriginCapture(
            lead_id=lead.id,
            source_interaction_id="leadgen-existing-customer",
            capture_fingerprint="c" * 64,
            capture_method="ad_lead_form_webhook",
            source_platform="meta",
            lead_source="Facebook Ads",
            external_campaign_id="campaign-1",
            external_form_id="form-1",
            captured_at=datetime.now(UTC),
            capture_source="meta.lead_ads_webhook",
            capture_reason="Verified Meta Lead",
        )
    )
    db_session.commit()
    event = Event(
        event_type=EventType.meta_lead_customer_match_reconciled,
        event_id=uuid4(),
        occurred_at=datetime.now(UTC),
        payload={
            "lead_id": str(lead.id),
            "status": "single_candidate",
            "candidate_count": 1,
        },
    )

    delivery = meta_lead_conversion.stage_conversion_for_event(db_session, event=event)

    assert delivery is not None
    assert delivery.payload_json["leadgen_id"] == "leadgen-existing-customer"
