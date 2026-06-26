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
)
from app.services.control_registry import Layer


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


def test_legacy_alias_still_honored(db_session):
    from app.models.domain_settings import SettingDomain

    _set_legacy(db_session, SettingDomain.billing, "autopay_enabled", False)
    assert control_registry.is_enabled(db_session, "billing.autopay") is False
    # other billing features unaffected
    assert control_registry.is_enabled(db_session, "billing.collections") is True


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


def test_env_overrides_canonical_row(db_session, monkeypatch):
    """Env is the emergency override and wins over a stored canonical row."""
    _set_canonical(db_session, "billing.autopay", False)
    monkeypatch.setenv("BILLING_AUTOPAY_ENABLED", "true")
    assert control_registry.is_enabled(db_session, "billing.autopay") is True


def test_disabled_components_covers_all_capture_features(db_session):
    _set_canonical(db_session, "billing.arrangements", False)
    _set_canonical(db_session, "billing.topup_reconciliation", False)
    disabled = billing_settings.disabled_billing_components(db_session)
    assert "billing.arrangements" in disabled
    assert "billing.topup_reconciliation" in disabled
    # default-OFF opt-in is not alarmed
    assert "billing.notifications_hourly" not in disabled
