"""Signed lead ingress, replay, and security-consequence guarantees."""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.lead_capture_webhooks import router
from app.db import get_db
from app.models.integration_platform import IntegrationInbox
from app.models.sales import Lead
from app.services.integrations.connectors.lead_capture_http import (
    LEAD_CAPTURE_CAPABILITY,
)
from tests.integration_platform_helpers import enable_capability

SIGNING_SECRET = "test-lead-capture-signing-secret"
SIGNATURE_HEADER = "x-test-lead-signature"
DELIVERY_HEADER = "x-test-lead-delivery"
SIGNATURE_PREFIX = "sha256="


def _app(db_session) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return app


def _binding(db_session, monkeypatch):
    monkeypatch.setenv("LEAD_CAPTURE_TEST_SIGNING_SECRET", SIGNING_SECRET)
    return enable_capability(
        db_session,
        connector_key="lead.capture.http",
        capability_id=LEAD_CAPTURE_CAPABILITY,
        config={
            "signature_header": SIGNATURE_HEADER,
            "delivery_id_header": DELIVERY_HEADER,
            "signature_prefix": SIGNATURE_PREFIX,
        },
        secret_refs={
            "webhook_signing_secret": "env://LEAD_CAPTURE_TEST_SIGNING_SECRET"
        },
    )


def _payload(delivery_id: str, *, title: str = "Abuja fibre enquiry") -> dict:
    return {
        "party": {"display_name": "Webhook Prospect", "contacts": []},
        "title": title,
        "lead_source": "Website",
        "origin": {
            "capture_method": "landing_page",
            "source_platform": "website",
            "source_interaction_id": delivery_id,
            "landing_path": "/fiber/abuja",
            "capture_source": "signed_http_connector",
            "capture_reason": "Signed interaction submitted to canonical capture",
        },
        "region": "FCT",
    }


def _post(client: TestClient, binding_id, payload: dict, delivery_id: str):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = (
        SIGNATURE_PREFIX
        + hmac.new(SIGNING_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    )
    return client.post(
        f"/api/v1/webhooks/lead-capture/{binding_id}",
        content=raw,
        headers={
            SIGNATURE_HEADER: signature,
            DELIVERY_HEADER: delivery_id,
            "content-type": "application/json",
        },
    )


def test_signed_capture_replay_has_one_receipt_and_lead(
    db_session, monkeypatch
) -> None:
    binding = _binding(db_session, monkeypatch)
    delivery_id = "landing-webhook-1"
    payload = _payload(delivery_id)

    with TestClient(_app(db_session)) as client:
        first = _post(client, binding.id, payload, delivery_id)
        replay = _post(client, binding.id, payload, delivery_id)

    assert first.status_code == 200
    assert first.json()["replayed"] is False
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["lead_id"] == first.json()["lead_id"]
    assert db_session.query(IntegrationInbox).count() == 1
    assert db_session.query(Lead).count() == 1


def test_invalid_signature_creates_no_receipt(db_session, monkeypatch) -> None:
    binding = _binding(db_session, monkeypatch)
    delivery_id = "landing-webhook-invalid-signature"
    raw = json.dumps(_payload(delivery_id), separators=(",", ":")).encode()

    with TestClient(_app(db_session)) as client:
        response = client.post(
            f"/api/v1/webhooks/lead-capture/{binding.id}",
            content=raw,
            headers={
                SIGNATURE_HEADER: f"{SIGNATURE_PREFIX}invalid",
                DELIVERY_HEADER: delivery_id,
                "content-type": "application/json",
            },
        )

    assert response.status_code == 401
    assert db_session.query(IntegrationInbox).count() == 0
    assert db_session.query(Lead).count() == 0


def test_identity_collision_persists_installation_quarantine(
    db_session, monkeypatch
) -> None:
    binding = _binding(db_session, monkeypatch)
    delivery_id = "landing-webhook-collision"

    with TestClient(_app(db_session)) as client:
        first = _post(client, binding.id, _payload(delivery_id), delivery_id)
        collision = _post(
            client,
            binding.id,
            _payload(delivery_id, title="Changed content under reused identity"),
            delivery_id,
        )

    assert first.status_code == 200
    assert collision.status_code == 409
    db_session.refresh(binding.installation)
    assert binding.installation.state == "quarantined"
    assert binding.installation.state_reason == "provider_event_identity_collision"
    assert db_session.query(IntegrationInbox).count() == 1
    assert db_session.query(Lead).count() == 1
