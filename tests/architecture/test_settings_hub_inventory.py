"""Source-only coverage checks; no application or database bootstrap required."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import (
    ChoiceLoader,
    DictLoader,
    Environment,
    FileSystemLoader,
    select_autoescape,
)

from app.services.settings_hub_catalog import (
    SettingsTab,
    section_by_id,
    setting_section,
)
from scripts.architecture.settings_hub_inventory import (
    build_register,
    declared_settings,
    source_fingerprint,
)

ROOT = Path(__file__).resolve().parents[2]


def test_fingerprint_ignores_checkout_line_endings_but_detects_content(
    tmp_path,
) -> None:
    source = tmp_path / "source.py"
    source.write_bytes(b"first = 1\r\nsecond = 2\r\n")
    windows = source_fingerprint(source)
    source.write_bytes(b"first = 1\nsecond = 2\n")
    assert source_fingerprint(source) == windows
    source.write_bytes(b"first = 1\nsecond = 3\n")
    assert source_fingerprint(source) != windows


def test_specialised_registered_fields_delegate_to_the_shared_form_writer() -> None:
    source = (ROOT / "app/services/web_system_config.py").read_text(encoding="utf-8")
    parsed = ast.parse(source)
    group = next(
        node
        for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name == "_save_settings_groups"
    )
    calls = {
        ast.unparse(node.func) for node in ast.walk(group) if isinstance(node, ast.Call)
    }
    assert "update_registered_settings" in calls
    assert not any(
        name.endswith((".upsert_by_key", ".stage_upsert_by_key")) for name in calls
    )
    monitoring = next(
        node
        for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name == "save_monitoring_config"
    )
    assert (
        sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_save_settings_groups"
            for node in ast.walk(monitoring)
        )
        == 1
    )


def test_error_render_preserves_inputs_and_redacts_secrets() -> None:
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"layouts/admin.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(ROOT / "templates"),
            ]
        ),
        autoescape=select_autoescape(),
    )
    env.globals["can"] = lambda _request, _permission: True

    def row(key, kind, value, secret=False):
        return SimpleNamespace(
            spec=SimpleNamespace(
                key=key,
                label=key,
                required_permission=None,
                value_type=SimpleNamespace(value=kind),
                allowed=None,
                min_value=None,
                max_value=None,
                required=False,
                is_secret=secret,
            ),
            value=value,
            configured=True,
            source="platform",
        )

    view = SimpleNamespace(
        section=section_by_id("network"),
        rows=(
            row("label", "string", "old label"),
            row("policy", "json", {"old": True}),
            row("flag", "boolean", True),
            row("password", "string", None, True),
        ),
    )
    markup = env.get_template("admin/system/settings_hub_editor.html").render(
        hub_view=view,
        categories=[],
        errors=("Invalid policy",),
        submitted_values={
            "label": "new label",
            "policy": "not valid JSON <tag>",
            "flag": False,
            "password": "must-never-be-echoed",
        },
        request=SimpleNamespace(state=SimpleNamespace(csrf_token="test")),
    )
    assert 'value="new label"' in markup
    assert "not valid JSON &lt;tag&gt;" in markup
    assert "must-never-be-echoed" not in markup
    assert "Value source: Platform" in markup
    checkbox = re.search(r'<input id="setting-flag"[^>]+>', markup)
    assert checkbox is not None and "checked" not in checkbox.group(0)
    assert 'name="flag" value="false"' in markup


def test_register_matches_the_complete_source_census() -> None:
    actual = json.loads(json.dumps(asdict(build_register(ROOT))))
    recorded = json.loads(
        (ROOT / "docs/settings-hub-migration-register.json").read_text(encoding="utf-8")
    )
    assert actual == recorded, (
        "Regenerate the migration register after changing a covered source."
    )


def test_every_active_identity_has_one_destination_and_retired_aliases_stay_retired() -> (
    None
):
    active, retired = declared_settings(ROOT)
    identities = {(row.domain, row.key) for row in active}
    assert len(identities) == len(active)
    assert identities.isdisjoint({(row.domain, row.key) for row in retired})
    assert {row.tab for row in active} == {tab.value for tab in SettingsTab}
    assert (
        setting_section(domain="radius", key="test_connection_default_hours").id
        == "test-connection"
    )
    assert (
        setting_section(domain="subscriber", key="referral_reward_amount").tab
        == SettingsTab.CATALOG
    )
    assert (
        setting_section(domain="auth", key="selfcare_domain").tab == SettingsTab.SYSTEM
    )
    with pytest.raises(KeyError):
        setting_section(domain="unassigned_future_domain", key="enabled")


def test_old_settings_route_family_and_template_are_removed() -> None:
    source = (ROOT / "app/web/admin/system.py").read_text(encoding="utf-8")
    paths = {
        decorator.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and decorator.args
        and isinstance(decorator.args[0], ast.Constant)
    }
    assert not any(
        isinstance(path, str) and (path == "/settings" or path.startswith("/settings/"))
        for path in paths
    )
    assert not (ROOT / "templates/admin/system/settings.html").exists()
    for folder in ("app", "templates"):
        for path in (ROOT / folder).rglob("*"):
            if path.suffix in {".py", ".html", ".js"}:
                assert not re.search(
                    r'/admin/system/settings(?:[?/]|["\'])',
                    path.read_text(encoding="utf-8"),
                ), path


def test_editor_renders_complex_values_and_escapes_untrusted_values() -> None:
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"layouts/admin.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(ROOT / "templates"),
            ]
        ),
        autoescape=select_autoescape(),
    )
    env.globals["can"] = lambda _request, _permission: True
    spec = SimpleNamespace(
        key="test_value",
        label="A setting",
        required_permission=None,
        value_type=SimpleNamespace(value="list"),
        allowed=None,
        min_value=None,
        max_value=None,
        required=False,
        is_secret=False,
    )
    view = SimpleNamespace(
        section=section_by_id("network"),
        rows=(SimpleNamespace(spec=spec, value=["<script>"], configured=True),),
    )
    markup = env.get_template("admin/system/settings_hub_editor.html").render(
        hub_view=view,
        categories=[],
        request=SimpleNamespace(state=SimpleNamespace(csrf_token="test")),
    )
    assert '<textarea id="setting-test_value"' in markup
    assert "\\u003cscript\\u003e" in markup
    assert "<script>" not in markup
    assert 'name="_csrf_token"' in markup


def test_register_expands_generic_actions_into_their_actual_tabs() -> None:
    register = build_register(ROOT)
    test_actions = [
        action
        for action in register.actions
        if action.path.endswith("/edit/test-connection")
    ]
    assert {action.method for action in test_actions} == {"GET", "POST"}
    assert {action.tab for action in test_actions} == {"network"}
    assert any(
        action.path.endswith("/email/smtp-senders/{sender_key}/test")
        for action in register.actions
    )


@pytest.mark.parametrize(
    ("canonical", "canonical_source", "legacy", "legacy_source", "expected"),
    [
        (False, "default", True, "platform", (True, "legacy force_2fa (platform)")),
        (False, "tenant", True, "platform", (False, "tenant")),
        (False, "default", False, "default", (False, "default")),
    ],
)
def test_mfa_editor_resolution_preserves_legacy_fallback(
    canonical, canonical_source, legacy, legacy_source, expected
) -> None:
    # Exercise the real resolver seam without importing the unavailable kernel package.
    source = ast.parse(
        (ROOT / "app/services/settings_spec.py").read_text(encoding="utf-8")
    )
    function = next(
        node
        for node in source.body
        if isinstance(node, ast.FunctionDef) and node.name == "resolve_with_source"
    )
    module = ast.Module(body=[function], type_ignores=[])
    calls = []

    def kernel(db, domain, key, *, tenant_id):
        calls.append((key, tenant_id))
        return (
            (legacy, legacy_source)
            if key == "force_2fa"
            else (canonical, canonical_source)
        )

    namespace = {
        "Session": object,
        "SettingDomain": SimpleNamespace(auth="auth", billing="billing"),
        "KernelSettingDomain": str,
        "get_spec": lambda *_args: object(),
        "operator_tenant_id": lambda: "operator-tenant",
        "kernel_resolve_with_source": kernel,
    }
    exec(compile(module, "settings_spec.py", "exec"), namespace)  # noqa: S102 -- trusted repository function, isolated dependencies
    assert (
        namespace["resolve_with_source"](object(), "auth", "admin_mfa_required")
        == expected
    )
    assert all(tenant == "operator-tenant" for _, tenant in calls)
    if canonical_source != "default":
        assert [key for key, _ in calls] == ["admin_mfa_required"]
