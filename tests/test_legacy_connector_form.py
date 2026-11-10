"""Legacy connector form: structured inputs and secret-safe presentation."""

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates

from app.models.connector import ConnectorAuthType, ConnectorConfig, ConnectorType
from app.schemas.connector import ConnectorConfigRead
from app.services import connector as connector_service
from app.services import web_integrations
from app.web.brand_globals import install_brand_jinja_global


def _command(**changes) -> web_integrations.ConnectorFormCommand:
    base = web_integrations.ConnectorFormCommand(
        name="Support connector",
        connector_type=ConnectorType.http,
        auth_type=ConnectorAuthType.basic,
        base_url="https://api.example.test",
        timeout_sec=30,
        username="operator",
        password="new-secret",
        token=None,
        api_key=None,
        secret=None,
        client_id=None,
        client_secret=None,
        header_names=("X-Region",),
        header_values=("lagos",),
        retry_max_attempts=3,
        retry_backoff_sec=10,
        notes="Used by support",
        is_active=True,
    )
    return replace(base, **changes)


def test_structured_form_builds_existing_connector_contract(db_session):
    connector = web_integrations.create_connector(db_session, _command())

    assert connector.auth_config == {"username": "operator", "password": "new-secret"}
    assert connector.headers == {"X-Region": "lagos"}
    assert connector.retry_policy == {"max_attempts": 3, "backoff_sec": 10}
    assert "new-secret" not in repr(_command())


def test_password_bytes_are_preserved(db_session):
    connector = web_integrations.create_connector(
        db_session, _command(name="Whitespace password", password=" leading space ")
    )
    assert connector.auth_config["password"] == " leading space "


def test_duplicate_name_has_a_form_error(db_session):
    web_integrations.create_connector(db_session, _command(name="Existing"))
    with pytest.raises(ValueError, match="already exists"):
        web_integrations.create_connector(db_session, _command(name="Existing"))


def test_legacy_list_uses_canonical_filter_and_page(db_session):
    web_integrations.create_connector(db_session, _command(name="Active support"))
    web_integrations.create_connector(
        db_session, _command(name="Disabled support", is_active=False)
    )
    result = connector_service.connector_configs.list_page(
        db_session,
        connector_service.connector_list_query(
            search="support", status="disabled", page=99
        ),
    )
    assert result.page_meta.total_items == 1
    assert result.query.page == 1
    assert [row.name for row in result.rows] == ["Disabled support"]


def test_form_rejects_credential_url_and_header_line_breaks(db_session):
    with pytest.raises(ValueError, match="without credentials"):
        web_integrations.create_connector(
            db_session, _command(base_url="https://user:password@api.example.test")
        )
    with pytest.raises(ValueError, match="line breaks"):
        web_integrations.create_connector(
            db_session, _command(header_values=("safe\r\nInjected: value",))
        )
    with pytest.raises(ValueError, match="username, password"):
        web_integrations.create_connector(
            db_session, _command(username=None, password=None)
        )
    assert web_integrations.connector_error_state(_command())["form"]["base_url"] == (
        "https://api.example.test"
    )
    assert (
        web_integrations.connector_error_state(
            _command(base_url="https://user:password@api.example.test")
        )["form"]["base_url"]
        == ""
    )


def test_masked_header_only_restores_the_original_name():
    assert web_integrations._unmask_secret_values(
        {"Authorization": web_integrations.SECRET_VALUE_SENTINEL},
        {"Authorization": "Bearer saved-token"},
    ) == {"Authorization": "Bearer saved-token"}
    with pytest.raises(ValueError, match="Enter a value"):
        web_integrations._unmask_secret_values(
            {"X-New-Token": web_integrations.SECRET_VALUE_SENTINEL},
            {"Authorization": "Bearer saved-token"},
        )
    form = web_integrations.connector_error_state(
        _command(
            header_names=("Authorization",),
            header_values=(web_integrations.SECRET_VALUE_SENTINEL,),
        )
    )["form"]
    assert form["header_rows"][0]["value"] == web_integrations.SECRET_VALUE_SENTINEL


def test_edit_keeps_legacy_endpoint_and_unexposed_metadata(db_session):
    connector = ConnectorConfig(
        name="Legacy database",
        connector_type=ConnectorType.custom,
        auth_type=ConnectorAuthType.basic,
        base_url="postgresql+psycopg://user:password@db.example.test/radius",
        auth_config={"username": "old", "password": "saved-secret"},
        retry_policy={"max_attempts": 3, "backoff_sec": 10, "jitter_sec": 2},
        metadata_={"target_name": "primary", "use_group": True},
    )
    db_session.add(connector)
    db_session.commit()

    edited = web_integrations.update_connector_config(
        db_session,
        str(connector.id),
        _command(
            name=None,
            connector_type=None,
            base_url=None,
            username=None,
            password=None,
            header_names=(),
            header_values=(),
            retry_max_attempts=None,
            retry_backoff_sec=None,
        ),
    )

    assert edited.base_url == connector.base_url
    assert edited.auth_config == {"username": "old", "password": "saved-secret"}
    assert edited.metadata_ == {"target_name": "primary", "use_group": True}
    assert edited.retry_policy == {"jitter_sec": 2}
    assert ConnectorConfigRead.model_validate(edited).model_dump()["base_url"] is None
    assert web_integrations.connector_edit_form(edited)["base_url"] == ""


def test_api_masks_sensitive_header_values(db_session):
    connector = web_integrations.create_connector(
        db_session,
        _command(
            header_names=("Authorization", "X-Region"),
            header_values=("Bearer private-token", "lagos"),
        ),
    )
    connector.metadata_ = {"api_key": "private", "region": "lagos"}
    connector.auth_config = {**connector.auth_config, "private_key": "private"}
    serialized = ConnectorConfigRead.model_validate(connector).model_dump()
    headers = serialized["headers"]
    assert headers == {"Authorization": "********", "X-Region": "********"}
    assert serialized["metadata_"] == {"api_key": "********", "region": "lagos"}
    assert serialized["auth_config"]["password"] == "********"
    assert serialized["auth_config"]["private_key"] == "********"


def test_legacy_connection_templates_have_no_json_editor():
    install_brand_jinja_global()
    env = Jinja2Templates(directory="templates").env
    for name in ("new.html", "detail.html"):
        env.get_template(f"admin/integrations/connectors/{name}")
        source = Path(f"templates/admin/integrations/connectors/{name}").read_text(
            encoding="utf-8"
        )
        assert "(JSON)" not in source
    env.get_template("admin/integrations/connectors/_fields.html")
