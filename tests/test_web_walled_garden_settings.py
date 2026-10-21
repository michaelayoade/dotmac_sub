"""Admin walled-garden allowed-resource page: service edits and HTTP routes."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.db import get_db
from app.models.audit import AuditEvent
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.router_management import (
    Router,
    RouterConfigSnapshot,
    RouterSnapshotSource,
)
from app.models.subscription_engine import SettingValueType
from app.schemas.walled_garden import (
    DEFAULT_WALLED_GARDEN_ALLOWED_RESOURCES,
    WalledGardenAllowedResources,
    WalledGardenResourceKind,
)
from app.services import domain_settings, settings_spec
from app.services import web_walled_garden_settings as service
from app.services.owner_commands import CommandContext
from app.web.admin import system as admin_system

KEY = "walled_garden_allowed_resources"
PAGE = "/admin/system/config/walled-garden"
PORTAL_URL = "https://selfcare.example.ng/portal/billing"
PORTAL_IP = "203.0.113.10"


def _context(reason: str = "pytest walled-garden edit") -> CommandContext:
    return CommandContext.system(
        actor="user:pytest-settings-admin",
        scope=domain_settings.ADMIN_SETTINGS_FORM_WRITE_SCOPE,
        reason=reason,
    )


def _put(db, key: str, value_type: SettingValueType, **values: object) -> None:
    db.query(DomainSetting).filter(
        DomainSetting.domain == SettingDomain.radius, DomainSetting.key == key
    ).delete(synchronize_session=False)
    db.add(
        DomainSetting(
            domain=SettingDomain.radius,
            key=key,
            value_type=value_type,
            is_active=True,
            **values,
        )
    )
    db.commit()


@pytest.fixture
def seeded(db_session):
    _put(
        db_session, "captive_portal_url", SettingValueType.string, value_text=PORTAL_URL
    )
    _put(db_session, "captive_portal_ip", SettingValueType.string, value_text=PORTAL_IP)
    _put(
        db_session,
        KEY,
        SettingValueType.json,
        value_json=copy.deepcopy(DEFAULT_WALLED_GARDEN_ALLOWED_RESOURCES),
    )
    return db_session


def _resources(db) -> WalledGardenAllowedResources:
    return WalledGardenAllowedResources.from_setting_value(
        settings_spec.resolve_value(db, SettingDomain.radius, KEY)
    )


def _fingerprint(db) -> str:
    return domain_settings.admin_setting_value_fingerprint(
        db, domain=SettingDomain.radius, key=KEY
    )


def _router_with_bare_snapshot(db, name: str = "SPDC") -> Router:
    router = Router(
        name=name,
        hostname=name.lower(),
        management_ip="192.0.2.1",
        rest_api_username="readonly",
        rest_api_password="not-used-in-tests",  # noqa: S106 - fixture only
        is_active=True,
    )
    db.add(router)
    db.flush()
    db.add(
        RouterConfigSnapshot(
            router_id=router.id,
            config_export="/ip firewall filter\nadd action=accept chain=forward\n",
            config_hash="0" * 64,
            source=RouterSnapshotSource.scheduled,
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    db.commit()
    return router


# --------------------------------------------------------------------------
# Service


def test_page_projects_entries_derived_portal_and_fingerprint(seeded) -> None:
    page = service.build_walled_garden_settings_page(seeded)

    assert [entry.key for entry in page.entries] == ["paystack"]
    assert page.entries[0].enabled is False
    assert page.portal.entry is not None
    assert page.portal.entry.derived is True
    assert page.portal.entry.hosts == (PORTAL_IP, "selfcare.example.ng")
    assert page.value_fingerprint == _fingerprint(seeded)
    assert page.stored_value_error is None
    assert page.form.mode is service.WalledGardenFormMode.add
    assert page.fleet.available is True
    assert page.fleet.routers == ()


def test_unconfigured_portal_is_reported_not_invented(db_session) -> None:
    page = service.build_walled_garden_settings_page(db_session)

    assert page.portal.entry is None
    assert page.portal.configuration_error


def test_add_entry_goes_through_the_settings_owner_with_audit(seeded) -> None:
    outcome = service.save_walled_garden_entry(
        seeded,
        command=service.SaveWalledGardenEntryCommand(
            context=_context("Walled garden: add allowed resource flutterwave"),
            expected_fingerprint=_fingerprint(seeded),
            draft=service.WalledGardenEntryDraft(
                label="Flutterwave",
                kind="payment",
                hosts=("Checkout.Flutterwave.com",),
            ),
        ),
    )

    assert outcome.action is service.WalledGardenEditAction.added
    assert outcome.entry_key == "flutterwave"
    assert outcome.updated_keys == ("radius.walled_garden_allowed_resources",)
    entries = _resources(seeded).entries
    assert [entry.key for entry in entries] == ["paystack", "flutterwave"]
    assert entries[1].hosts == ("checkout.flutterwave.com",)
    assert entries[1].enabled is False
    audit = (
        seeded.query(AuditEvent)
        .filter(AuditEvent.action == "control.settings_form_updated")
        .one()
    )
    assert audit.metadata_["setting_keys"] == ["radius.walled_garden_allowed_resources"]
    assert audit.metadata_["reason"] == (
        "Walled garden: add allowed resource flutterwave"
    )
    assert "hosts" not in str(audit.metadata_)


def test_invalid_entry_returns_field_errors_and_writes_nothing(seeded) -> None:
    before = _fingerprint(seeded)
    with pytest.raises(service.WalledGardenEditError) as excinfo:
        service.save_walled_garden_entry(
            seeded,
            command=service.SaveWalledGardenEntryCommand(
                context=_context(),
                expected_fingerprint=before,
                draft=service.WalledGardenEntryDraft(
                    label="Bad",
                    kind="portal",
                    hosts=("1.2.3.4",),
                    key="Bad Key",
                ),
            ),
        )

    fields = excinfo.value.field_errors
    assert set(fields) == {
        service.WalledGardenEntryField.key,
        service.WalledGardenEntryField.kind,
        service.WalledGardenEntryField.hosts,
    }
    assert "IP literals" in fields[service.WalledGardenEntryField.hosts]
    assert _fingerprint(seeded) == before


def test_duplicate_key_is_a_key_field_error(seeded) -> None:
    with pytest.raises(service.WalledGardenEditError) as excinfo:
        service.save_walled_garden_entry(
            seeded,
            command=service.SaveWalledGardenEntryCommand(
                context=_context(),
                expected_fingerprint=_fingerprint(seeded),
                draft=service.WalledGardenEntryDraft(
                    label="Paystack two",
                    kind="payment",
                    hosts=("pay.example.com",),
                    key="paystack",
                ),
            ),
        )

    assert service.WalledGardenEntryField.key in excinfo.value.field_errors


def test_edit_keeps_key_and_enabled_state(seeded) -> None:
    service.set_walled_garden_entry_enabled(
        seeded,
        command=service.SetWalledGardenEntryEnabledCommand(
            context=_context(),
            expected_fingerprint=_fingerprint(seeded),
            key="paystack",
            enabled=True,
        ),
    )

    outcome = service.save_walled_garden_entry(
        seeded,
        command=service.SaveWalledGardenEntryCommand(
            context=_context(),
            expected_fingerprint=_fingerprint(seeded),
            original_key="paystack",
            draft=service.WalledGardenEntryDraft(
                label="Paystack checkout",
                kind="payment",
                hosts=("checkout.paystack.com",),
                key="ignored-on-edit",
            ),
        ),
    )

    assert outcome.action is service.WalledGardenEditAction.updated
    (entry,) = _resources(seeded).entries
    assert entry.key == "paystack"
    assert entry.label == "Paystack checkout"
    assert entry.hosts == ("checkout.paystack.com",)
    assert entry.enabled is True


def test_toggle_and_remove(seeded) -> None:
    service.set_walled_garden_entry_enabled(
        seeded,
        command=service.SetWalledGardenEntryEnabledCommand(
            context=_context(),
            expected_fingerprint=_fingerprint(seeded),
            key="paystack",
            enabled=True,
        ),
    )
    assert _resources(seeded).entries[0].enabled is True

    outcome = service.remove_walled_garden_entry(
        seeded,
        command=service.RemoveWalledGardenEntryCommand(
            context=_context(),
            expected_fingerprint=_fingerprint(seeded),
            key="paystack",
        ),
    )

    assert outcome.action is service.WalledGardenEditAction.removed
    assert _resources(seeded).entries == ()


def test_stale_fingerprint_is_refused_by_the_owner(seeded) -> None:
    stale = _fingerprint(seeded)
    service.set_walled_garden_entry_enabled(
        seeded,
        command=service.SetWalledGardenEntryEnabledCommand(
            context=_context(),
            expected_fingerprint=stale,
            key="paystack",
            enabled=True,
        ),
    )

    with pytest.raises(service.WalledGardenEditError) as excinfo:
        service.remove_walled_garden_entry(
            seeded,
            command=service.RemoveWalledGardenEntryCommand(
                context=_context(),
                expected_fingerprint=stale,
                key="paystack",
            ),
        )

    assert excinfo.value.error_code is service.WalledGardenEditErrorCode.STALE
    assert [entry.key for entry in _resources(seeded).entries] == ["paystack"]


def test_unknown_entry_is_not_found(seeded) -> None:
    with pytest.raises(service.WalledGardenEditError) as excinfo:
        service.set_walled_garden_entry_enabled(
            seeded,
            command=service.SetWalledGardenEntryEnabledCommand(
                context=_context(),
                expected_fingerprint=_fingerprint(seeded),
                key="nope",
                enabled=True,
            ),
        )

    assert excinfo.value.error_code is service.WalledGardenEditErrorCode.ENTRY_NOT_FOUND


def test_fleet_summary_reports_missing_enabled_entries(seeded) -> None:
    _router_with_bare_snapshot(seeded, "SPDC")
    service.set_walled_garden_entry_enabled(
        seeded,
        command=service.SetWalledGardenEntryEnabledCommand(
            context=_context(),
            expected_fingerprint=_fingerprint(seeded),
            key="paystack",
            enabled=True,
        ),
    )

    fleet = service.build_fleet_summary(seeded)

    (row,) = fleet.routers
    assert row.router_name == "SPDC"
    assert row.status_label == "Not ready"
    assert "paystack" in row.missing_entry_keys
    assert fleet.routers_missing_enabled_entries == 1
    assert fleet.ready_router_count == 0


def test_unavailable_readiness_is_distinct_from_no_routers(seeded) -> None:
    with patch.object(
        service,
        "resolve_fleet_walled_garden_readiness",
        side_effect=RuntimeError("boom"),
    ):
        fleet = service.build_fleet_summary(seeded)

    assert fleet.available is False
    assert fleet.routers == ()


def test_hosts_text_and_key_helpers() -> None:
    assert service.parse_hosts_text(
        "a.example.com\n b.example.com, c.example.com "
    ) == (
        "a.example.com",
        "b.example.com",
        "c.example.com",
    )
    assert service.suggested_entry_key("  Moniepoint POS!  ") == "moniepoint-pos"
    assert WalledGardenResourceKind.portal not in service.EDITABLE_KINDS


# --------------------------------------------------------------------------
# HTTP routes


def _client(db_session, *, permission_keys: set[str]) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        request.state.auth = {
            "principal_type": "system_user",
            "principal_id": "00000000-0000-0000-0000-00000000a11e",
            "permission_keys": permission_keys,
        }
        request.state.csrf_token = "pytest-csrf"
        return await call_next(request)

    app.include_router(admin_system.router, prefix="/admin")
    app.dependency_overrides[get_db] = lambda: db_session
    for route in admin_system.router.routes:
        for dependency in getattr(route, "dependencies", ()):
            if dependency.dependency is not None:
                app.dependency_overrides[dependency.dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(seeded):
    with (
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
    ):
        yield _client(seeded, permission_keys={"system:settings:write"})


def test_page_renders_entries_portal_and_readiness(client, seeded) -> None:
    _router_with_bare_snapshot(seeded, "SPDC")

    response = client.get(PAGE)

    assert response.status_code == 200
    body = response.text
    assert "Walled Garden" in body
    assert "Paystack" in body and "checkout.paystack.com" in body
    assert "Derived from Captive Portal URL and IP" in body
    assert PORTAL_IP in body
    assert "SPDC" in body and "Not ready" in body
    assert 'name="expected_fingerprint"' in body
    assert "Add resource" in body


def test_read_only_viewer_sees_no_edit_controls(seeded) -> None:
    with (
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
    ):
        viewer = _client(seeded, permission_keys={"system:settings:read"})
        response = viewer.get(PAGE)

    assert response.status_code == 200
    assert "Paystack" in response.text
    assert 'id="entry-form"' not in response.text
    assert "/entries/paystack/enabled" not in response.text


def test_add_route_saves_and_redirects(client, seeded) -> None:
    response = client.post(
        f"{PAGE}/entries",
        data={
            "expected_fingerprint": _fingerprint(seeded),
            "label": "Flutterwave",
            "kind": "payment",
            "hosts": "checkout.flutterwave.com\napi.flutterwave.com",
            "enabled": "true",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"{PAGE}?notice=added&entry=flutterwave"
    added = _resources(seeded).entries[-1]
    assert added.key == "flutterwave"
    assert added.enabled is True
    assert added.hosts == ("checkout.flutterwave.com", "api.flutterwave.com")


def test_add_route_shows_inline_validation_errors(client, seeded) -> None:
    before = _fingerprint(seeded)

    response = client.post(
        f"{PAGE}/entries",
        data={
            "expected_fingerprint": before,
            "label": "Bad hosts",
            "kind": "payment",
            "hosts": "https://checkout.example.com",
        },
    )

    assert response.status_code == 400
    assert 'id="wg-hosts-error"' in response.text
    assert 'aria-invalid="true"' in response.text
    assert "https://checkout.example.com" in response.text
    assert _fingerprint(seeded) == before


def test_toggle_route_enables_entry(client, seeded) -> None:
    response = client.post(
        f"{PAGE}/entries/paystack/enabled",
        data={"expected_fingerprint": _fingerprint(seeded), "enabled": "true"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "notice=enabled" in response.headers["location"]
    assert _resources(seeded).entries[0].enabled is True


def test_toggle_route_rejects_ambiguous_value(client, seeded) -> None:
    response = client.post(
        f"{PAGE}/entries/paystack/enabled",
        data={"expected_fingerprint": _fingerprint(seeded), "enabled": "on"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert _resources(seeded).entries[0].enabled is False


def test_remove_route_with_stale_fingerprint_is_conflict(client, seeded) -> None:
    response = client.post(
        f"{PAGE}/entries/paystack/remove",
        data={"expected_fingerprint": "0" * 64},
    )

    assert response.status_code == 409
    assert "changed after the page was loaded" in response.text
    assert [entry.key for entry in _resources(seeded).entries] == ["paystack"]


def test_remove_route_removes_entry(client, seeded) -> None:
    response = client.post(
        f"{PAGE}/entries/paystack/remove",
        data={"expected_fingerprint": _fingerprint(seeded)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _resources(seeded).entries == ()


def test_routes_require_system_settings_permissions() -> None:
    from tests.test_admin_route_permissions import _route_has_permission

    assert _route_has_permission(
        admin_system.router,
        "/system/config/walled-garden",
        "GET",
        "system:settings:read",
    )
    for path in (
        "/system/config/walled-garden/entries",
        "/system/config/walled-garden/entries/{entry_key}/enabled",
        "/system/config/walled-garden/entries/{entry_key}/remove",
    ):
        assert _route_has_permission(
            admin_system.router, path, "POST", "system:settings:write"
        ), path
