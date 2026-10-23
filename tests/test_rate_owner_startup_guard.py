"""A production process must not serve before the governed rate switch has run."""

from __future__ import annotations

import pytest

from app import main as app_main
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.services import access_rate_profile as owner


def _flag(db, value: bool) -> None:
    db.add(
        DomainSetting(
            domain=SettingDomain.radius,
            key="rate_owner_enabled",
            value_type=SettingValueType.boolean,
            value_json=value,
            is_active=True,
        )
    )
    db.commit()


def test_assert_switched_on_refuses_when_the_flag_is_off(db_session):
    with pytest.raises(owner.RateOwnerNotSwitchedOn, match="governed rate switch"):
        owner.assert_switched_on(db_session)


def test_assert_switched_on_passes_when_the_flag_is_on(db_session):
    _flag(db_session, True)
    owner.assert_switched_on(db_session)


def _env(monkeypatch, value: str) -> None:
    import dataclasses

    from app import config

    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, app_env=value)
    )


@pytest.fixture()
def borrowed(db_session, monkeypatch):
    monkeypatch.setattr(app_main, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "rollback", lambda: None)
    monkeypatch.setattr(db_session, "close", lambda: None)


@pytest.mark.parametrize("env", ["prod", "production"])
def test_production_startup_refuses_while_the_flag_is_off(
    db_session, borrowed, monkeypatch, env
):
    _env(monkeypatch, env)
    with pytest.raises(owner.RateOwnerNotSwitchedOn):
        app_main._assert_rate_owner_switched_on()


def test_production_startup_serves_once_the_switch_has_run(
    db_session, borrowed, monkeypatch
):
    _env(monkeypatch, "production")
    _flag(db_session, True)
    app_main._assert_rate_owner_switched_on()


@pytest.mark.parametrize("env", ["development", "test", "staging"])
def test_other_environments_are_not_blocked(db_session, borrowed, monkeypatch, env):
    _env(monkeypatch, env)
    app_main._assert_rate_owner_switched_on()
