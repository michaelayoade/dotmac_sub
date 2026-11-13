"""HTTP adapter checks for the integrations work surfaces."""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from app.db import get_db
from app.services import connector as connector_service
from app.services import web_integrations
from app.services.list_query import PageMeta
from app.web.admin import integrations as routes


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router, prefix="/admin")
    app.dependency_overrides[get_db] = object
    for route in routes.router.routes:
        for dependency in getattr(route, "dependencies", ()):
            app.dependency_overrides[dependency.dependency] = lambda: None
    monkeypatch.setattr(
        routes,
        "_base_context",
        lambda request, db, active_page: {"request": request},
    )
    monkeypatch.setattr(
        routes.templates,
        "TemplateResponse",
        lambda name, context, status_code=200: Response(name, status_code=status_code),
    )
    return TestClient(app, follow_redirects=False)


def test_root_redirects_to_installed_and_activity_is_scoped(client):
    assert client.get("/admin/integrations/").headers["location"] == (
        "/admin/integrations/installed"
    )
    installation_id = uuid4()
    row = {"installation": SimpleNamespace(id=installation_id, name="Webhook")}
    list_query = web_integrations.installed_list_query()
    with patch.object(
        routes.web_integrations_service,
        "build_installed_integrations_data",
        return_value={
            "integrations": [row],
            "activity_installation": row,
            "activity_log": [],
            "list_query": list_query,
        },
    ) as build:
        assert (
            client.get(
                f"/admin/integrations/installed?activity_for={installation_id}"
            ).status_code
            == 200
        )
        assert build.call_args.kwargs["activity_installation_id"] == installation_id
    with patch.object(
        routes.web_integrations_service,
        "build_installed_integrations_data",
        return_value={
            "integrations": [row],
            "activity_installation": None,
            "activity_log": [],
            "list_query": list_query,
        },
    ):
        assert (
            client.get(
                f"/admin/integrations/installed?activity_for={uuid4()}"
            ).status_code
            == 404
        )


def test_installed_route_canonicalizes_out_of_range_page(client):
    with patch.object(
        routes.web_integrations_service,
        "build_installed_integrations_data",
        return_value={
            "activity_installation": None,
            "list_query": web_integrations.installed_list_query(page=1),
        },
    ):
        response = client.get("/admin/integrations/installed?page=99")
    assert response.status_code == 303
    assert "page=1" in response.headers["location"]


def test_legacy_list_passes_canonical_filters(client):
    def list_page(db, query):
        return connector_service.ConnectorListPage(
            rows=(), query=query, page_meta=PageMeta.from_query(query, 0)
        )

    with patch.object(
        routes.connector_service.connector_configs, "list_page", side_effect=list_page
    ) as listing:
        response = client.get(
            "/admin/integrations/connectors?search=Legacy&status=disabled"
        )
        assert response.status_code == 200
        query = listing.call_args.args[1]
        assert query.search == "Legacy"
        assert query.filter_value("status") == "disabled"


def test_catalogue_and_legacy_editor_routes_render(client):
    with patch.object(
        routes.web_integrations_service,
        "build_marketplace_data",
        return_value={"marketplace_cards": []},
    ):
        assert client.get("/admin/integrations/marketplace").status_code == 200
    assert client.get("/admin/integrations/connectors/new").status_code == 200
    with patch.object(routes.connector_service.connector_configs, "get") as get:
        get.return_value = SimpleNamespace(
            auth_type=SimpleNamespace(value="none"),
            timeout_sec=None,
            notes=None,
            is_active=True,
        )
        assert (
            client.get(f"/admin/integrations/connectors/{uuid4()}").status_code == 200
        )


def test_create_and_edit_accept_structured_form_fields(client):
    connector_id = uuid4()
    with patch.object(
        routes.web_integrations_service,
        "create_connector",
        return_value=SimpleNamespace(id=connector_id),
    ) as create:
        response = client.post(
            "/admin/integrations/connectors",
            data={
                "name": "Legacy connection",
                "connector_type": "http",
                "auth_type": "basic",
                "base_url": "https://legacy.example.test",
                "auth_username": "operator",
                "auth_password": " password ",
                "header_name": "X-Region",
                "header_value": "lagos",
                "retry_max_attempts": "3",
                "is_active": "true",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"].endswith(str(connector_id))
        command = create.call_args.kwargs["command"]
        assert isinstance(command, web_integrations.ConnectorFormCommand)
        assert command.password == " password "
        assert command.header_names == ("X-Region",)

    with patch.object(
        routes.web_integrations_service, "update_connector_config"
    ) as update:
        response = client.post(
            f"/admin/integrations/connectors/{connector_id}",
            data={"auth_type": "none", "is_active": "true"},
        )
        assert response.status_code == 303
        assert update.call_args.kwargs["connector_id"] == str(connector_id)


def test_installation_toggle_passes_selected_state(client):
    installation_id = uuid4()
    with patch.object(
        routes.web_integrations_service, "bulk_set_integrations_enabled"
    ) as toggle:
        response = client.post(
            f"/admin/integrations/installed/{installation_id}/toggle",
            data={"enabled": "true"},
        )
        assert response.status_code == 303
        assert toggle.call_args.kwargs == {
            "connector_ids": [str(installation_id)],
            "enabled": True,
        }
        bulk = client.post(
            "/admin/integrations/installed/bulk",
            data={"connector_ids": str(installation_id), "action": "disable"},
        )
        assert bulk.status_code == 303
        assert toggle.call_args.kwargs == {
            "connector_ids": [str(installation_id)],
            "enabled": False,
        }
        assert (
            client.post(
                "/admin/integrations/installed/bulk", data={"action": "bogus"}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/admin/integrations/installed/bulk", data={"action": "enable"}
            ).headers["location"]
            == "/admin/integrations/installed"
        )
        assert toggle.call_count == 2


def test_old_embed_bookmark_redirects_to_connection_detail(client):
    response = client.get(f"/admin/integrations/connectors/{uuid4()}/embed")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/integrations/connectors/")
