from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.field.config import router as field_config_router
from app.db import get_db
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.services.auth_dependencies import require_user_auth
from app.services.field.config import (
    DEFAULT_FEATURE_FLAGS,
    DEFAULT_LATEST_APP_VERSION,
    DEFAULT_MIN_APP_VERSION,
    field_config,
)


def _client(db_session) -> TestClient:
    app = FastAPI()
    app.include_router(field_config_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_defaults_when_no_settings_exist(db_session):
    config = field_config.get(db_session)
    assert config["min_app_version"] == DEFAULT_MIN_APP_VERSION
    assert config["latest_app_version"] == DEFAULT_LATEST_APP_VERSION
    assert config["feature_flags"] == DEFAULT_FEATURE_FLAGS


def test_domain_settings_override_defaults(db_session):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.field,
            key="mobile_min_app_version",
            value_type=SettingValueType.string,
            value_text="2.3.0",
        )
    )
    db_session.add(
        DomainSetting(
            domain=SettingDomain.field,
            key="mobile_feature_flags",
            value_type=SettingValueType.json,
            value_json={"location_sharing": True, "beta_surveys": True},
        )
    )
    db_session.commit()

    config = field_config.get(db_session)

    assert config["min_app_version"] == "2.3.0"
    assert config["latest_app_version"] == DEFAULT_LATEST_APP_VERSION
    assert config["feature_flags"]["location_sharing"] is True
    assert config["feature_flags"]["beta_surveys"] is True
    assert config["feature_flags"]["vendor_module"] is True


def test_inactive_setting_rows_are_ignored(db_session):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.field,
            key="mobile_min_app_version",
            value_type=SettingValueType.string,
            value_text="9.9.9",
            is_active=False,
        )
    )
    db_session.commit()

    assert field_config.get(db_session)["min_app_version"] == DEFAULT_MIN_APP_VERSION


def test_config_route_is_public(db_session):
    app = FastAPI()
    app.include_router(field_config_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session

    routes = [
        route for route in field_config_router.routes if isinstance(route, APIRoute)
    ]
    assert any(route.path == "/field/config" for route in routes)
    for route in routes:
        dependency_calls = [
            dependency.call for dependency in route.dependant.dependencies
        ]
        assert require_user_auth not in dependency_calls

    resp = TestClient(app).get("/api/v1/field/config")
    assert resp.status_code == 200
    assert resp.json()["min_app_version"] == DEFAULT_MIN_APP_VERSION


def test_field_config_router_registered_without_auth():
    from app.main import _DEFERRED_API_ROUTER_SPECS

    assert (
        "app.api.field.config",
        "router",
        "api",
        "none",
    ) in _DEFERRED_API_ROUTER_SPECS
