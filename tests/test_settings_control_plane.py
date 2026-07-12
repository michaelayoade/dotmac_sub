from __future__ import annotations

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingCreate, DomainSettingUpdate
from app.services.domain_settings import DomainSettings
from app.services.settings_health import deactivate_retired_settings, inspect_settings
from app.services.settings_seed import seed_registered_settings
from app.services.settings_spec import SettingSource, resolve_setting, resolve_value


def _row(
    domain: SettingDomain,
    key: str,
    value: str,
    *,
    active: bool = True,
) -> DomainSetting:
    return DomainSetting(
        domain=domain,
        key=key,
        value_type=SettingValueType.string,
        value_text=value,
        is_active=active,
    )


def test_database_value_wins_over_bootstrap_environment(db_session, monkeypatch):
    monkeypatch.setenv("BILLING_DEFAULT_CURRENCY", "USD")
    db_session.add(_row(SettingDomain.billing, "default_currency", "NGN"))
    db_session.commit()

    resolved = resolve_setting(
        db_session,
        SettingDomain.billing,
        "default_currency",
    )

    assert resolved.value == "NGN"
    assert resolved.source is SettingSource.database


def test_inactive_setting_falls_back_to_environment(db_session, monkeypatch):
    monkeypatch.setenv("BILLING_DEFAULT_CURRENCY", "USD")
    db_session.add(
        _row(
            SettingDomain.billing,
            "default_currency",
            "NGN",
            active=False,
        )
    )
    db_session.commit()

    assert resolve_value(db_session, SettingDomain.billing, "default_currency") == "USD"


def test_registered_secret_is_classified_by_the_registry(db_session):
    service = DomainSettings(SettingDomain.auth)
    created = service.create(
        db_session,
        DomainSettingCreate(
            domain=SettingDomain.auth,
            key="jwt_secret",
            value_type=SettingValueType.string,
            value_text="test-secret",
            is_secret=False,
        ),
    )

    assert created.is_secret is True


def test_bulk_upsert_commits_the_domain_once(db_session, monkeypatch):
    service = DomainSettings(SettingDomain.billing)
    db_session.add_all(
        [
            _row(SettingDomain.billing, "default_currency", "NGN"),
            _row(SettingDomain.billing, "company_name", "Dotmac"),
        ]
    )
    db_session.commit()
    commit_calls = 0
    original_commit = db_session.commit

    def counted_commit():
        nonlocal commit_calls
        commit_calls += 1
        return original_commit()

    monkeypatch.setattr(db_session, "commit", counted_commit)
    service.upsert_many_by_key(
        db_session,
        {
            "default_currency": DomainSettingUpdate(value_text="USD"),
            "company_name": DomainSettingUpdate(value_text="Changed"),
        },
    )

    assert commit_calls == 1
    assert service.get_by_key(db_session, "default_currency").value_text == "USD"
    assert service.get_by_key(db_session, "company_name").value_text == "Changed"


def test_registry_seed_covers_all_domains_in_one_lifecycle(db_session):
    created = seed_registered_settings(db_session)

    assert created > 0
    assert (
        db_session.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.field)
        .filter(DomainSetting.key == "completion_requires_evidence")
        .one()
        .value_text
        == "true"
    )
    assert (
        db_session.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.projects)
        .count()
        > 0
    )


def test_settings_health_reports_unknown_runtime_rows(db_session):
    db_session.add(_row(SettingDomain.billing, "unowned_runtime_toggle", "true"))
    db_session.commit()

    report = inspect_settings(db_session)

    assert report.ok is False
    assert "billing.unowned_runtime_toggle" in report.unknown_active


def test_retired_setting_cleanup_is_dry_run_then_soft_deletes(db_session):
    row = _row(SettingDomain.catalog, "default_olt_port_type", "gpon")
    db_session.add(row)
    db_session.commit()

    identity = "catalog.default_olt_port_type"
    assert deactivate_retired_settings(db_session) == (identity,)
    assert row.is_active is True

    assert deactivate_retired_settings(db_session, apply=True) == (identity,)
    db_session.refresh(row)
    assert row.is_active is False
