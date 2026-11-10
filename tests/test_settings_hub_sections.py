"""Runtime checks for section save isolation and register parity."""

from __future__ import annotations

import json
from pathlib import Path

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import (
    settings_spec,
    web_system_settings_forms,
    web_system_settings_hub,
)
from app.services.owner_commands import CommandContext
from app.services.settings_hub_catalog import setting_section


def test_static_census_matches_the_effective_runtime_registry() -> None:
    register = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/settings-hub-migration-register.json"
        ).read_text(encoding="utf-8")
    )
    recorded = {
        (row["domain"], row["key"])
        for row in register["settings"]
        if row["kind"] == "registered"
    }
    assert recorded == {
        (str(spec.domain), spec.key) for spec in settings_spec.SETTINGS_SPECS
    }


def test_saving_test_duration_never_writes_other_radius_settings(
    db_session, monkeypatch
):
    captured = []
    monkeypatch.setattr(
        web_system_settings_forms,
        "_apply_prepared_settings",
        lambda **kwargs: captured.extend(kwargs["updates"]) or (),
    )
    result = web_system_settings_forms.update_hub_section(
        db_session,
        command=web_system_settings_forms.HubSettingsUpdateCommand(
            section_id="test-connection",
            values={
                "test_connection_default_hours": "3",
                "test_connection_maximum_hours": "12",
                "test_connection_deadline_verified": "true",
                "_csrf_token": "test",
            },
        ),
        context=CommandContext.system(
            actor="user:pytest-settings-admin",
            scope="control:settings:write",
            reason="test section isolation",
        ),
    )
    assert result.errors == ()
    assert {update.key for update in captured} == {
        "test_connection_default_hours",
        "test_connection_maximum_hours",
        "test_connection_deadline_verified",
    }
    assert all(update.domain == SettingDomain.radius for update in captured)


def test_out_of_section_fields_refuse_the_entire_submission(db_session, monkeypatch):
    def refuse_write(**kwargs):
        raise AssertionError("Invalid section submission reached the writer")

    monkeypatch.setattr(
        web_system_settings_forms, "_apply_prepared_settings", refuse_write
    )
    result = web_system_settings_forms.update_hub_section(
        db_session,
        command=web_system_settings_forms.HubSettingsUpdateCommand(
            section_id="test-connection", values={"coa_retries": "10"}
        ),
        context=CommandContext.system(
            actor="user:pytest-settings-admin",
            scope="control:settings:write",
            reason="test unknown field",
        ),
    )
    assert result.errors


def test_blank_secret_is_skipped_and_protected_switch_is_not_in_generic_batch(
    db_session, monkeypatch
):
    captured = []
    secret = settings_spec.SettingSpec(
        domain=SettingDomain.radius,
        key="test_secret",
        env_var=None,
        value_type=SettingValueType.string,
        default="",
        is_secret=True,
    )
    protected = settings_spec.SettingSpec(
        domain=SettingDomain.radius,
        key="protected",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=False,
        required_permission="billing:starter_ledger_live:write",
    )
    monkeypatch.setattr(
        web_system_settings_hub, "section_specs", lambda **kwargs: (secret, protected)
    )
    monkeypatch.setattr(
        web_system_settings_forms,
        "_apply_prepared_settings",
        lambda **kwargs: captured.extend(kwargs["updates"]) or (),
    )
    result = web_system_settings_forms.update_hub_section(
        db_session,
        command=web_system_settings_forms.HubSettingsUpdateCommand(
            section_id="test-connection",
            values={"test_secret": "", "protected": "true"},
        ),
        context=CommandContext.system(
            actor="user:pytest-settings-admin",
            scope="control:settings:write",
            reason="test protected fields",
        ),
    )
    assert result.errors == ()
    assert captured == []


def test_every_setting_is_reachable_from_its_hub_tab(db_session):
    categories = web_system_settings_hub.build_settings_hub_context(db_session)[
        "categories"
    ]
    urls = {
        category["id"]: {link["url"] for link in category["links"]}
        for category in categories
    }
    for spec in settings_spec.SETTINGS_SPECS:
        section = setting_section(domain=str(spec.domain), key=spec.key)
        assert section.url in urls[section.tab.value], (spec.domain, spec.key)


def test_only_changed_values_create_tenant_overrides(db_session, monkeypatch):
    default = settings_spec.get_spec(
        SettingDomain.radius, "test_connection_default_hours"
    )
    maximum = settings_spec.get_spec(
        SettingDomain.radius, "test_connection_maximum_hours"
    )
    assert default is not None and maximum is not None
    captured = []
    monkeypatch.setattr(
        settings_spec,
        "resolve_with_source",
        lambda _db, _domain, key: (3 if key == default.key else 24, "default"),
    )
    monkeypatch.setattr(
        web_system_settings_forms,
        "_apply_prepared_settings",
        lambda **kwargs: captured.extend(kwargs["updates"]) or (),
    )
    result = web_system_settings_forms.update_registered_settings(
        db_session,
        command=web_system_settings_forms.RegisteredSettingsUpdateCommand(
            specs=(default, maximum), values={default.key: "3", maximum.key: "12"}
        ),
        context=CommandContext.system(
            actor="user:pytest-settings-admin",
            scope="control:settings:write",
            reason="test inherited values",
        ),
    )
    assert result.errors == ()
    assert [update.key for update in captured] == [maximum.key]


def test_missing_field_is_not_an_instruction_to_disable_it(db_session, monkeypatch):
    spec = settings_spec.get_spec(
        SettingDomain.radius, "test_connection_deadline_verified"
    )
    assert spec is not None
    captured = []
    monkeypatch.setattr(
        web_system_settings_forms,
        "_apply_prepared_settings",
        lambda **kwargs: captured.extend(kwargs["updates"]) or (),
    )
    result = web_system_settings_forms.update_registered_settings(
        db_session,
        command=web_system_settings_forms.RegisteredSettingsUpdateCommand(
            specs=(spec,), values={}
        ),
        context=CommandContext.system(
            actor="user:pytest-settings-admin",
            scope="control:settings:write",
            reason="test absent field",
        ),
    )
    assert result.errors == () and captured == []


def test_error_state_keeps_only_submitted_non_secret_values():
    public = settings_spec.get_spec(
        SettingDomain.radius, "test_connection_default_hours"
    )
    secret = settings_spec.get_spec(SettingDomain.notification, "smtp_password")
    assert public is not None and secret is not None
    state = web_system_settings_forms.safe_submitted_values(
        specs=(public, secret),
        values={
            public.key: "invalid",
            secret.key: "replacement-secret",
            "unknown": "ignored",
        },
    )
    assert state == {public.key: "invalid"}


def test_specialised_multi_domain_save_uses_one_registered_batch(
    db_session, monkeypatch
):
    from app.services import web_system_config
    from tests.settings_hub_fixtures import settings_context

    calls = []

    def writer(_db, *, command, context):
        calls.append(command)
        return web_system_settings_forms.HubSettingsUpdateResult(errors=())

    monkeypatch.setattr(web_system_config, "update_registered_settings", writer)
    monkeypatch.setattr(
        db_session,
        "commit",
        lambda: (_ for _ in ()).throw(
            AssertionError("Registered settings bypassed their owner")
        ),
    )
    web_system_config._save_settings_groups(
        db_session,
        data={
            "interface_walk_interval_seconds": "400",
            "cleanup_interval_seconds": "4000",
        },
        groups=(
            web_system_config.ConfigSettingGroup(
                domain=SettingDomain.snmp, keys=("interface_walk_interval_seconds",)
            ),
            web_system_config.ConfigSettingGroup(
                domain=SettingDomain.bandwidth, keys=("cleanup_interval_seconds",)
            ),
        ),
        context=settings_context(),
    )
    assert len(calls) == 1
    assert {str(spec.domain) for spec in calls[0].specs} == {"snmp", "bandwidth"}
