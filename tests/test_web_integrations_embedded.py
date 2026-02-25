import uuid

import pytest
from fastapi import HTTPException

from app.models.connector import ConnectorAuthType, ConnectorType
from app.schemas.connector import ConnectorConfigCreate
from app.services import connector as connector_service
from app.services import web_integrations as web_integrations_service


def test_build_embedded_connector_data_ready(db_session):
    connector = connector_service.connector_configs.create(
        db_session,
        ConnectorConfigCreate(
            name="QuickBooks",
            connector_type=ConnectorType.custom,
            auth_type=ConnectorAuthType.none,
            base_url="https://example.com/quickbooks",
        ),
    )
    state = web_integrations_service.build_embedded_connector_data(
        db_session, connector_id=str(connector.id)
    )
    assert state["health_status"] == "ready"
    assert state["embed_url"] == "https://example.com/quickbooks"


def test_build_embedded_connector_data_misconfigured(db_session):
    connector = connector_service.connector_configs.create(
        db_session,
        ConnectorConfigCreate(
            name="Broken",
            connector_type=ConnectorType.custom,
            auth_type=ConnectorAuthType.none,
            base_url="localhost:8080/no-scheme",
        ),
    )
    state = web_integrations_service.build_embedded_connector_data(
        db_session, connector_id=str(connector.id)
    )
    assert state["health_status"] == "misconfigured"
    assert state["embed_url"] == ""


def test_build_embedded_connector_data_404(db_session):
    with pytest.raises(HTTPException) as exc:
        web_integrations_service.build_embedded_connector_data(
            db_session, connector_id=str(uuid.uuid4())
        )
    assert exc.value.status_code == 404


def test_build_embedded_connector_data_auth_required_on_probe(db_session, monkeypatch):
    connector = connector_service.connector_configs.create(
        db_session,
        ConnectorConfigCreate(
            name="Auth service",
            connector_type=ConnectorType.custom,
            auth_type=ConnectorAuthType.none,
            base_url="https://example.com/protected",
        ),
    )

    class _Resp:
        status_code = 403

    monkeypatch.setattr(web_integrations_service.httpx, "get", lambda *args, **kwargs: _Resp())
    state = web_integrations_service.build_embedded_connector_data(
        db_session, connector_id=str(connector.id), perform_check=True
    )
    assert state["health_status"] == "auth_required"
    assert state["health_http_status"] == 403
    assert state["probe_checked"] is True


def test_build_embedded_connector_data_unreachable_on_probe_error(db_session, monkeypatch):
    connector = connector_service.connector_configs.create(
        db_session,
        ConnectorConfigCreate(
            name="Down service",
            connector_type=ConnectorType.custom,
            auth_type=ConnectorAuthType.none,
            base_url="https://down.example.com",
        ),
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(web_integrations_service.httpx, "get", _boom)
    state = web_integrations_service.build_embedded_connector_data(
        db_session, connector_id=str(connector.id), perform_check=True
    )
    assert state["health_status"] == "unreachable"
    assert state["health_http_status"] is None
    assert state["probe_checked"] is True
