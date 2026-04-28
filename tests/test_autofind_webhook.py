"""Tests for autofind webhook API endpoint."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.autofind_webhook import router
from app.db import get_db
from app.models.network import OLTDevice
from app.services.autofind_trigger import AutofindTriggerResult


@pytest.fixture
def test_app(db_session):
    """Create a test app with just the autofind webhook router."""
    app = FastAPI()

    # Override get_db dependency to use test session
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    """Create test client."""
    return TestClient(test_app)


@pytest.fixture
def sample_olt(db_session):
    """Create a sample OLT for testing."""
    olt = OLTDevice(
        name="Webhook-Test-OLT",
        mgmt_ip="10.0.0.200",
        is_active=True,
    )
    db_session.add(olt)
    db_session.commit()
    return olt


class TestWebhookTriggerEndpoint:
    """Tests for POST /api/v1/autofind/webhook/trigger."""

    def test_trigger_success(self, client, db_session, sample_olt):
        """Test successful webhook trigger."""
        mock_result = AutofindTriggerResult(
            triggered=True,
            olt_id=str(sample_olt.id),
            olt_name="Webhook-Test-OLT",
            task_id="task-webhook-123",
        )

        with patch(
            "app.api.autofind_webhook.trigger_autofind_by_identifier",
            return_value=mock_result,
        ):
            response = client.post(
                "/api/v1/autofind/webhook/trigger",
                json={"olt": str(sample_olt.id)},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["triggered"] is True
        assert data["olt_id"] == str(sample_olt.id)
        assert data["task_id"] == "task-webhook-123"

    def test_trigger_by_ip(self, client, db_session, sample_olt):
        """Test webhook trigger by IP address."""
        mock_result = AutofindTriggerResult(
            triggered=True,
            olt_id=str(sample_olt.id),
            olt_name="Webhook-Test-OLT",
            task_id="task-ip-123",
        )

        with patch(
            "app.api.autofind_webhook.trigger_autofind_by_identifier",
            return_value=mock_result,
        ):
            response = client.post(
                "/api/v1/autofind/webhook/trigger",
                json={"olt": "10.0.0.200"},
            )

        assert response.status_code == 200
        assert response.json()["triggered"] is True

    def test_trigger_by_name(self, client, db_session, sample_olt):
        """Test webhook trigger by OLT name."""
        mock_result = AutofindTriggerResult(
            triggered=True,
            olt_id=str(sample_olt.id),
            olt_name="Webhook-Test-OLT",
            task_id="task-name-123",
        )

        with patch(
            "app.api.autofind_webhook.trigger_autofind_by_identifier",
            return_value=mock_result,
        ):
            response = client.post(
                "/api/v1/autofind/webhook/trigger",
                json={"olt": "Webhook-Test-OLT"},
            )

        assert response.status_code == 200
        assert response.json()["triggered"] is True

    def test_trigger_with_force(self, client, db_session, sample_olt):
        """Test webhook trigger with force=true."""
        mock_result = AutofindTriggerResult(
            triggered=True,
            olt_id=str(sample_olt.id),
            olt_name="Webhook-Test-OLT",
            task_id="task-force-123",
        )

        with patch(
            "app.api.autofind_webhook.trigger_autofind_by_identifier",
            return_value=mock_result,
        ) as mock_trigger:
            response = client.post(
                "/api/v1/autofind/webhook/trigger",
                json={"olt": str(sample_olt.id), "force": True},
            )

        assert response.status_code == 200
        # Verify force was passed to the trigger function
        call_kwargs = mock_trigger.call_args[1]
        assert call_kwargs.get("force") is True

    def test_trigger_with_source(self, client, db_session, sample_olt):
        """Test webhook trigger with custom source."""
        mock_result = AutofindTriggerResult(
            triggered=True,
            olt_id=str(sample_olt.id),
            olt_name="Webhook-Test-OLT",
            task_id="task-src-123",
        )

        with patch(
            "app.api.autofind_webhook.trigger_autofind_by_identifier",
            return_value=mock_result,
        ) as mock_trigger:
            response = client.post(
                "/api/v1/autofind/webhook/trigger",
                json={"olt": str(sample_olt.id), "source": "zabbix"},
            )

        assert response.status_code == 200
        call_kwargs = mock_trigger.call_args[1]
        assert call_kwargs.get("source") == "zabbix"

    def test_trigger_olt_not_found(self, client, db_session):
        """Test webhook trigger when OLT not found."""
        mock_result = AutofindTriggerResult(
            triggered=False,
            reason="No active OLT found matching 'unknown-olt'",
        )

        with patch(
            "app.api.autofind_webhook.trigger_autofind_by_identifier",
            return_value=mock_result,
        ):
            response = client.post(
                "/api/v1/autofind/webhook/trigger",
                json={"olt": "unknown-olt"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["triggered"] is False
        assert "no active olt found" in data["message"].lower()

    def test_trigger_in_cooldown(self, client, db_session, sample_olt):
        """Test webhook trigger when OLT is in cooldown."""
        mock_result = AutofindTriggerResult(
            triggered=False,
            olt_id=str(sample_olt.id),
            olt_name="Webhook-Test-OLT",
            reason="OLT Webhook-Test-OLT is in cooldown period",
        )

        with patch(
            "app.api.autofind_webhook.trigger_autofind_by_identifier",
            return_value=mock_result,
        ):
            response = client.post(
                "/api/v1/autofind/webhook/trigger",
                json={"olt": str(sample_olt.id)},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["triggered"] is False
        assert "cooldown" in data["message"].lower()

    def test_trigger_invalid_payload(self, client):
        """Test webhook trigger with invalid payload."""
        response = client.post(
            "/api/v1/autofind/webhook/trigger",
            json={},  # Missing required 'olt' field
        )

        assert response.status_code == 422  # Validation error

    def test_trigger_extra_fields_rejected(self, client):
        """Test that extra fields in payload are rejected."""
        response = client.post(
            "/api/v1/autofind/webhook/trigger",
            json={"olt": "test-olt", "unknown_field": "value"},
        )

        assert response.status_code == 422  # Validation error


class TestWebhookAuthentication:
    """Tests for webhook authentication."""

    def test_missing_token_when_required(self, db_session):
        """Test that missing token returns 401 when token is configured."""
        with patch.dict(os.environ, {"AUTOFIND_WEBHOOK_TOKEN": "secret-token"}):
            # Need to reload the module to pick up env var
            import importlib
            import app.api.autofind_webhook as webhook_module
            importlib.reload(webhook_module)

            # Create new app with reloaded module
            app = FastAPI()
            app.dependency_overrides[get_db] = lambda: db_session
            app.include_router(webhook_module.router, prefix="/api/v1")
            client = TestClient(app)

            response = client.post(
                "/api/v1/autofind/webhook/trigger",
                json={"olt": "test-olt"},
                # No X-Autofind-Token header
            )

            assert response.status_code == 401

            # Cleanup - reload without token
            with patch.dict(os.environ, {"AUTOFIND_WEBHOOK_TOKEN": ""}, clear=False):
                importlib.reload(webhook_module)

    def test_invalid_token(self, db_session):
        """Test that invalid token returns 401."""
        with patch.dict(os.environ, {"AUTOFIND_WEBHOOK_TOKEN": "secret-token"}):
            import importlib
            import app.api.autofind_webhook as webhook_module
            importlib.reload(webhook_module)

            app = FastAPI()
            app.dependency_overrides[get_db] = lambda: db_session
            app.include_router(webhook_module.router, prefix="/api/v1")
            client = TestClient(app)

            response = client.post(
                "/api/v1/autofind/webhook/trigger",
                json={"olt": "test-olt"},
                headers={"X-Autofind-Token": "wrong-token"},
            )

            assert response.status_code == 401

            # Cleanup
            with patch.dict(os.environ, {"AUTOFIND_WEBHOOK_TOKEN": ""}, clear=False):
                importlib.reload(webhook_module)

    def test_valid_token(self, db_session, sample_olt):
        """Test that valid token allows access."""
        with patch.dict(os.environ, {"AUTOFIND_WEBHOOK_TOKEN": "secret-token"}):
            import importlib
            import app.api.autofind_webhook as webhook_module
            importlib.reload(webhook_module)

            app = FastAPI()
            app.dependency_overrides[get_db] = lambda: db_session
            app.include_router(webhook_module.router, prefix="/api/v1")
            client = TestClient(app)

            mock_result = AutofindTriggerResult(
                triggered=True,
                olt_id=str(sample_olt.id),
                olt_name="Webhook-Test-OLT",
                task_id="task-auth-123",
            )

            with patch(
                "app.api.autofind_webhook.trigger_autofind_by_identifier",
                return_value=mock_result,
            ):
                response = client.post(
                    "/api/v1/autofind/webhook/trigger",
                    json={"olt": str(sample_olt.id)},
                    headers={"X-Autofind-Token": "secret-token"},
                )

            assert response.status_code == 200
            assert response.json()["triggered"] is True

            # Cleanup
            with patch.dict(os.environ, {"AUTOFIND_WEBHOOK_TOKEN": ""}, clear=False):
                importlib.reload(webhook_module)


class TestWebhookHealthEndpoint:
    """Tests for GET /api/v1/autofind/webhook/health."""

    def test_health_check(self, client):
        """Test health check endpoint."""
        response = client.get("/api/v1/autofind/webhook/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "autofind-webhook"
        assert "token_configured" in data

    def test_health_check_shows_token_not_configured(self, client):
        """Test that health check shows token not configured."""
        # Default state - no token configured
        response = client.get("/api/v1/autofind/webhook/health")

        assert response.status_code == 200
        data = response.json()
        assert data["token_configured"] is False
