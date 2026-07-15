"""Guard the grace and captive-access ownership boundaries."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_retired_prepaid_timer_settings_have_no_runtime_reader():
    runtime = "\n".join(
        path.read_text()
        for path in (ROOT / "app").rglob("*.py")
        if path.name != "settings_seed.py"
    )
    assert '"prepaid_grace_days"' not in runtime
    assert '"prepaid_deactivation_days"' not in runtime


def test_access_decision_modules_do_not_consume_raw_captive_flag():
    for relative in (
        "app/services/access_resolution.py",
        "app/services/customer_service_state.py",
        "app/services/radius_access_state.py",
        "app/services/radius_projection_planner.py",
        "app/services/connectivity_reconciler.py",
    ):
        assert "captive_redirect_enabled" not in _read(relative), relative


def test_radius_and_connectivity_writers_consume_canonical_restriction():
    for relative in (
        "app/services/radius.py",
        "app/services/radius_population.py",
        "app/services/events/handlers/enforcement.py",
        "app/services/connectivity_reconciler.py",
    ):
        assert "resolve_subscription_restriction" in _read(relative), relative


def test_financial_owner_persists_access_mode_and_grace_evidence():
    lifecycle = _read("app/services/account_lifecycle.py")
    financial = _read("app/services/collections/_core.py")
    assert "access_mode=access_mode" in lifecycle
    assert '"grace_decision": grace_decision' in financial
    assert "resolve_walled_garden_decision" in financial


def test_admin_policy_form_has_no_parallel_suspension_action_control():
    service = _read("app/services/web_catalog_settings.py")
    template = _read("templates/admin/catalog/settings/policy_set_form.html")
    assert 'form_str("suspension_action"' not in service
    assert '"suspension_action": SuspensionAction' not in service
    assert 'name="suspension_action"' not in template


def test_checked_in_specs_declare_hard_reject_default():
    for relative in (
        "docs/SOT_RELATIONSHIP_MAP.md",
        "docs/designs/CONNECTIVITY_STATE_MACHINE.md",
        "docs/radius_state_refactor/phase0_state_model.md",
    ):
        content = _read(relative).lower()
        assert "hard reject" in content, relative
        assert "captive-by-default" not in content, relative
        assert "captive` by default" not in content, relative
