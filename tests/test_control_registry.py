"""Optional capability controls exclude the customer-financial lifecycle."""

import pytest

from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.services import control_registry, module_manager, scheduler_config
from app.services.control_registry import Layer


def _set_row(db, domain, key, value: bool) -> None:
    db.add(
        DomainSetting(
            domain=domain,
            key=key,
            value_type=SettingValueType.boolean,
            value_text="true" if value else "false",
            value_json=value,
            is_active=True,
        )
    )
    db.commit()


def _set_canonical(db, canonical_key: str, value: bool) -> None:
    _set_row(
        db,
        SettingDomain.modules,
        canonical_key.replace(".", "_"),
        value,
    )


@pytest.fixture(autouse=True)
def _clear_module_cache():
    module_manager.invalidate_module_cache()
    yield
    module_manager.invalidate_module_cache()


def test_optional_features_resolve_to_declared_defaults(db_session):
    for control in control_registry.all_controls():
        if control.layer is Layer.feature:
            assert (
                control_registry.is_enabled(db_session, control.key)
                is control.on_missing
            )


def test_optional_feature_composes_with_owner_module(db_session):
    assert control_registry.is_enabled(db_session, "network.ont_reconcile") is True
    module_manager.update_module_flags(db_session, payload={"network": False})
    assert control_registry.is_enabled(db_session, "network") is False
    assert control_registry.is_enabled(db_session, "network.ont_reconcile") is False


def test_optional_canonical_row_changes_optional_feature(db_session):
    assert control_registry.is_enabled(db_session, "network.ont_reconcile") is True
    _set_canonical(db_session, "network.ont_reconcile", False)
    assert control_registry.is_enabled(db_session, "network.ont_reconcile") is False


def test_retired_alias_does_not_change_optional_control(db_session):
    _set_row(db_session, SettingDomain.radius, "coa_enabled", False)
    assert control_registry.is_enabled(db_session, "access.radius_coa") is True


@pytest.mark.parametrize(
    "key",
    [
        "billing",
        "billing.autopay",
        "billing.collections",
        "billing.prepaid_service_renewals",
        "collections.prepaid_balance_enforcement",
        "catalog.subscription_expiration",
        "notifications.queue",
        "customer.services_view",
    ],
)
def test_customer_financial_lifecycle_is_not_registered(key):
    assert key not in {control.key for control in control_registry.all_controls()}


def test_stale_financial_rows_cannot_disable_runtime_resolution(db_session):
    _set_row(db_session, SettingDomain.modules, "billing_autopay", False)
    _set_row(db_session, SettingDomain.billing, "autopay_enabled", False)
    assert control_registry.is_enabled(db_session, "billing.autopay") is True


def test_canonical_writer_rejects_financial_lifecycle_keys(db_session):
    with pytest.raises(ValueError, match="Unknown feature controls"):
        control_registry.update_canonical_feature_controls(
            db_session,
            payload={"billing.autopay": False},
        )


def test_scheduler_routes_registered_optional_keys_through_resolver(db_session):
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
