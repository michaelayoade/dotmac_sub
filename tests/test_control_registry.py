"""The single control-plane resolver: modules → features, fail-open, parity.

These guard the 2026-06-26 "billing always-on / one control plane" change:
- the resolver is behavior-neutral vs the legacy per-key defaults (parity),
- a feature is off iff its module is off (single-master composition),
- the scheduler chokepoint routes through the one resolver.
"""

import pytest

from app.models.domain_settings import DomainSetting, SettingValueType
from app.services import (
    billing_settings,
    control_registry,
    module_manager,
    scheduler_config,
    settings_spec,
)
from app.services.control_registry import Layer

_UNREGISTERED_LEGACY_ENV = ("USAGE_RATING_ENABLED",)


def _set_row(db, domain, key, value: bool):
    db.add(
        DomainSetting(
            domain=domain,
            key=key,
            value_type=SettingValueType.boolean,
            value_text="true" if value else "false",
            is_active=True,
        )
    )
    db.commit()


def _set_legacy(db, domain, key, value: bool):
    _set_row(db, domain, key, value)


def _set_canonical(db, canonical_key: str, value: bool):
    """Write the canonical control row a registry-driven admin page would set."""
    from app.models.domain_settings import SettingDomain

    _set_row(db, SettingDomain.modules, canonical_key.replace(".", "_"), value)


@pytest.fixture(autouse=True)
def _clear_module_cache():
    module_manager.invalidate_module_cache()
    from app.services.settings_cache import SettingsCache

    SettingsCache.invalidate("modules", "feature_states")
    yield
    module_manager.invalidate_module_cache()
    SettingsCache.invalidate("modules", "feature_states")


@pytest.fixture(autouse=True)
def _clear_control_env(monkeypatch):
    """Keep unregistered compatibility-path assertions host independent.

    Registered controls deliberately ignore every retired alias environment
    variable. The cleanup remains useful for the one unregistered legacy key
    asserted by this module and documents the complete retired inventory.
    """
    for control in control_registry.all_controls():
        for alias in getattr(control, "legacy", ()):
            if alias.env:
                monkeypatch.delenv(alias.env, raising=False)
    for env_name in _UNREGISTERED_LEGACY_ENV:
        monkeypatch.delenv(env_name, raising=False)


def test_resolver_parity_with_legacy_defaults(db_session):
    """With no rows and all modules on, every feature resolves to its declared
    on_missing — which is set to the legacy _effective_bool default. A mismatch
    (the bug class that caused the outage) fails here."""
    for control in control_registry.all_controls():
        if control.layer is not Layer.feature:
            continue
        assert (
            control_registry.is_enabled(db_session, control.key) is control.on_missing
        ), f"{control.key} drifted from its legacy default"


def test_feature_off_when_module_off(db_session):
    # billing.autopay defaults ON...
    assert control_registry.is_enabled(db_session, "billing.autopay") is True
    # ...but turning the billing MODULE off disables every billing feature.
    module_manager.update_module_flags(db_session, payload={"billing": False})
    assert control_registry.is_enabled(db_session, "billing") is False
    assert control_registry.is_enabled(db_session, "billing.autopay") is False
    assert control_registry.is_enabled(db_session, "billing.collections") is False


def test_legacy_database_alias_is_ignored_after_cutover(db_session):
    from app.models.domain_settings import SettingDomain

    _set_legacy(db_session, SettingDomain.billing, "autopay_enabled", False)
    assert control_registry.is_enabled(db_session, "billing.autopay") is True
    resolution = control_registry.resolve_control(db_session, "billing.autopay")
    assert resolution.source == "registry default"


def test_legacy_environment_alias_is_ignored_after_cutover(db_session, monkeypatch):
    monkeypatch.setenv("BILLING_AUTOPAY_ENABLED", "false")

    assert control_registry.is_enabled(db_session, "billing.autopay") is True
    resolution = control_registry.resolve_control(db_session, "billing.autopay")
    assert resolution.source == "registry default"


def test_retired_aliases_are_not_settings_registry_writers():
    from app.models.domain_settings import SettingDomain

    retained_non_alias = (SettingDomain.billing, "billing_enabled")
    for control in control_registry.all_controls():
        for alias in control.legacy:
            if (alias.domain, alias.key) == retained_non_alias:
                continue
            assert settings_spec.get_spec(alias.domain, alias.key) is None, (
                f"{alias.domain.value}.{alias.key} still exposes a legacy writer"
            )


def test_scheduler_chokepoint_routes_through_resolver(db_session):
    from app.models.domain_settings import SettingDomain

    # Same answer as the resolver for a registered key...
    assert (
        scheduler_config._effective_bool(
            db_session,
            SettingDomain.billing,
            "autopay_enabled",
            "BILLING_AUTOPAY_ENABLED",
            True,
        )
        is True
    )
    # ...and module-off disables the scheduled task via the chokepoint.
    module_manager.update_module_flags(db_session, payload={"billing": False})
    assert (
        scheduler_config._effective_bool(
            db_session,
            SettingDomain.billing,
            "autopay_enabled",
            "BILLING_AUTOPAY_ENABLED",
            True,
        )
        is False
    )


def test_unregistered_key_keeps_legacy_behavior(db_session):
    from app.models.domain_settings import SettingDomain

    # A key with no control returns the passed default (legacy path preserved).
    assert (
        scheduler_config._effective_bool(
            db_session,
            SettingDomain.usage,
            "usage_rating_enabled",
            "USAGE_RATING_ENABLED",
            True,
        )
        is True
    )
    assert (
        control_registry.control_for_legacy(SettingDomain.usage, "usage_rating_enabled")
        is None
    )


def test_billing_enabled_master_composes_module(db_session):
    assert billing_settings.billing_enabled(db_session) is True
    module_manager.update_module_flags(db_session, payload={"billing": False})
    assert billing_settings.billing_enabled(db_session) is False


def test_canonical_row_disables_feature_and_scheduler(db_session):
    """A registry-driven admin page sets the canonical row; both the resolver
    AND the scheduler chokepoint must honor it (the gap the review caught)."""
    from app.models.domain_settings import SettingDomain

    assert control_registry.is_enabled(db_session, "billing.autopay") is True
    _set_canonical(db_session, "billing.autopay", False)
    assert control_registry.is_enabled(db_session, "billing.autopay") is False
    # Scheduler path agrees.
    assert (
        scheduler_config._effective_bool(
            db_session,
            SettingDomain.billing,
            "autopay_enabled",
            "BILLING_AUTOPAY_ENABLED",
            True,
        )
        is False
    )


def test_canonical_row_disables_collections(db_session):
    _set_canonical(db_session, "billing.collections", False)
    assert control_registry.is_enabled(db_session, "billing.collections") is False


def test_default_off_feature_stays_off_until_enabled(db_session):
    # network.olt_profile_sync is fail-CLOSED (on_missing=False).
    assert control_registry.is_enabled(db_session, "network.olt_profile_sync") is False
    _set_canonical(db_session, "network.olt_profile_sync", True)
    assert control_registry.is_enabled(db_session, "network.olt_profile_sync") is True


def test_retired_alias_rows_do_not_change_registered_controls(db_session):
    from app.models.domain_settings import SettingDomain

    assert (
        control_registry.is_enabled(db_session, "billing.direct_bank_transfer") is False
    )
    _set_legacy(db_session, SettingDomain.billing, "direct_bank_transfer_enabled", True)
    assert (
        control_registry.is_enabled(db_session, "billing.direct_bank_transfer") is False
    )

    assert control_registry.is_enabled(db_session, "access.radius_coa") is True
    _set_legacy(db_session, SettingDomain.radius, "coa_enabled", False)
    assert control_registry.is_enabled(db_session, "access.radius_coa") is True


def test_scheduler_routes_new_registered_usage_keys_through_resolver(db_session):
    from app.models.domain_settings import SettingDomain

    assert (
        scheduler_config._effective_bool(
            db_session,
            SettingDomain.usage,
            "radius_accounting_import_enabled",
            "RADIUS_ACCOUNTING_IMPORT_ENABLED",
            True,
        )
        is True
    )

    _set_canonical(db_session, "sessions.radius_accounting_import", False)
    assert (
        scheduler_config._effective_bool(
            db_session,
            SettingDomain.usage,
            "radius_accounting_import_enabled",
            "RADIUS_ACCOUNTING_IMPORT_ENABLED",
            True,
        )
        is False
    )


def test_retired_env_cannot_override_canonical_row(db_session, monkeypatch):
    _set_canonical(db_session, "billing.autopay", False)
    monkeypatch.setenv("BILLING_AUTOPAY_ENABLED", "true")
    assert control_registry.is_enabled(db_session, "billing.autopay") is False

    resolution = control_registry.resolve_control(db_session, "billing.autopay")
    assert resolution.enabled is False
    assert resolution.source == "database (modules.billing_autopay)"
    assert resolution.affected_scope == "billing module / billing.autopay capability"
    assert resolution.canonical_value is False


def test_canonical_writer_ignores_retired_environment_alias(db_session, monkeypatch):
    monkeypatch.setenv("BILLING_AUTOPAY_ENABLED", "true")

    changes = control_registry.update_canonical_feature_controls(
        db_session, payload={"billing.autopay": False}
    )
    resolution = control_registry.resolve_control(db_session, "billing.autopay")

    assert changes[0]["stored"] == {"from": None, "to": False}
    assert changes[0]["effective"] == {"from": True, "to": False}
    assert resolution.canonical_value is False
    assert resolution.enabled is False
    assert resolution.source == "database (modules.billing_autopay)"


def test_canonical_writer_inherit_restores_registry_default(db_session):
    from app.models.domain_settings import SettingDomain

    _set_legacy(db_session, SettingDomain.billing, "autopay_enabled", False)
    control_registry.update_canonical_feature_controls(
        db_session, payload={"billing.autopay": True}
    )
    assert control_registry.is_enabled(db_session, "billing.autopay") is True

    changes = control_registry.update_canonical_feature_controls(
        db_session, payload={"billing.autopay": None}
    )
    resolution = control_registry.resolve_control(db_session, "billing.autopay")

    assert changes[0]["stored"] == {"from": True, "to": None}
    assert resolution.canonical_value is None
    assert resolution.enabled is True
    assert resolution.source == "registry default"


def test_canonical_writer_rejects_unregistered_controls(db_session):
    with pytest.raises(ValueError, match="Unknown feature controls"):
        control_registry.update_canonical_feature_controls(
            db_session, payload={"billing.imaginary": True}
        )


def test_resolution_explains_module_composition(db_session):
    module_manager.update_module_flags(db_session, payload={"billing": False})

    resolution = control_registry.resolve_control(db_session, "billing.autopay")

    assert resolution.enabled is False
    assert resolution.own_enabled is True
    assert resolution.module_enabled is False
    assert resolution.source.startswith("owner module billing disabled")


def test_work_order_pull_defaults_on_and_flips_via_canonical_row(db_session):
    """Phase 2 flip lever: crm.work_order_pull fails OPEN (inert until flipped)
    and the canonical row turns it off — both through the resolver and
    through the scheduler chokepoint that gates work_order_mirror_reconcile."""
    from app.models.domain_settings import SettingDomain

    assert control_registry.is_enabled(db_session, "crm.work_order_pull") is True
    assert (
        scheduler_config._effective_bool(
            db_session,
            SettingDomain.scheduler,
            "crm_work_order_pull_enabled",
            "CRM_WORK_ORDER_PULL_ENABLED",
            True,
        )
        is True
    )

    _set_canonical(db_session, "crm.work_order_pull", False)
    assert control_registry.is_enabled(db_session, "crm.work_order_pull") is False
    assert (
        scheduler_config._effective_bool(
            db_session,
            SettingDomain.scheduler,
            "crm_work_order_pull_enabled",
            "CRM_WORK_ORDER_PULL_ENABLED",
            True,
        )
        is False
    )


def test_disabled_components_covers_all_capture_features(db_session):
    _set_canonical(db_session, "billing.arrangements", False)
    _set_canonical(db_session, "billing.topup_reconciliation", False)
    disabled = billing_settings.disabled_billing_components(db_session)
    assert "billing.arrangements" in disabled
    assert "billing.topup_reconciliation" in disabled
    # default-OFF opt-in is not alarmed
    assert "billing.notifications_hourly" not in disabled
