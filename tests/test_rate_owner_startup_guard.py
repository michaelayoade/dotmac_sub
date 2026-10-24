"""No process may serve or project before the governed rate switch has run.

The web app, the Celery workers and the one RADIUS writer all enforce it, and the
environment check fails closed: only an explicit development/test value exempts.
"""

from __future__ import annotations

import pytest

from app import main as app_main
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.services import access_rate_profile as owner
from app.services import radius_population


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


def deployed(monkeypatch, env: str = "production") -> None:
    """Run as a deployed process with the given declared environment."""
    import dataclasses

    from app import config

    monkeypatch.setattr(owner, "_under_test", lambda: False)
    monkeypatch.setattr(
        config,
        "settings",
        dataclasses.replace(config.settings, app_env_declared=env.strip().lower()),
    )


@pytest.fixture()
def borrowed(db_session, monkeypatch):
    monkeypatch.setattr(app_main, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "rollback", lambda: None)
    monkeypatch.setattr(db_session, "close", lambda: None)


# --- P2: the environment check fails closed ---------------------------------


@pytest.mark.parametrize(
    "env", ["development", "dev", "test", "testing", "ci", "local", " DEV ", "CI"]
)
def test_only_explicit_dev_and_test_environments_are_exempt(monkeypatch, env):
    deployed(monkeypatch, env)
    assert owner.switch_guard_applies() is False


@pytest.mark.parametrize(
    "env", ["production", "prod", "staging", "shadow", "stage", "qa", "prodution", ""]
)
def test_every_other_value_including_unset_is_guarded(monkeypatch, env):
    deployed(monkeypatch, env)
    assert owner.switch_guard_applies() is True


def test_the_declared_environment_is_empty_when_unset():
    """A clean process: the default hides the omission, the declared value not."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"APP_ENV", "ENVIRONMENT"}
    }
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(root), env.get("PYTHONPATH")])
    )
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from app.config import settings;"
            "print(repr(settings.app_env_declared), settings.app_env)",
            str(root),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert result.stdout.split() == ["''", "development"]


def test_a_pytest_run_is_exempt(monkeypatch):
    import dataclasses

    from app import config

    monkeypatch.setattr(
        config,
        "settings",
        dataclasses.replace(config.settings, app_env_declared="production"),
    )
    assert owner._under_test() is True
    assert owner.switch_guard_applies() is False


# --- the flag itself ---------------------------------------------------------


def test_assert_switched_on_refuses_when_the_flag_is_off(db_session):
    with pytest.raises(owner.RateOwnerNotSwitchedOn, match="governed rate switch"):
        owner.assert_switched_on(db_session)


def test_assert_switched_on_passes_when_the_flag_is_on(db_session):
    _flag(db_session, True)
    owner.assert_switched_on(db_session)


# --- the web process ---------------------------------------------------------


@pytest.mark.parametrize("env", ["prod", "production", "staging", "shadow", ""])
def test_web_startup_refuses_while_the_flag_is_off(
    db_session, borrowed, monkeypatch, env
):
    deployed(monkeypatch, env)
    with pytest.raises(owner.RateOwnerNotSwitchedOn):
        app_main._assert_rate_owner_switched_on()


def test_web_startup_serves_once_the_switch_has_run(db_session, borrowed, monkeypatch):
    deployed(monkeypatch)
    _flag(db_session, True)
    app_main._assert_rate_owner_switched_on()


def test_dev_startup_is_not_blocked(db_session, borrowed, monkeypatch):
    deployed(monkeypatch, "development")
    app_main._assert_rate_owner_switched_on()


# --- P1: workers and the one writer -----------------------------------------


def test_the_radius_writer_refuses_a_projection_while_the_flag_is_off(
    db_session, monkeypatch
):
    deployed(monkeypatch)
    with pytest.raises(owner.RateOwnerNotSwitchedOn):
        radius_population.populate(dry_run=False, source_db=db_session)
    with pytest.raises(owner.RateOwnerNotSwitchedOn):
        radius_population.reconcile_usernames(
            {"someone"}, dry_run=False, source_db=db_session
        )


def test_a_dry_run_projection_is_not_blocked(db_session, monkeypatch):
    deployed(monkeypatch)
    """The switch's baseline is a dry run and must work before the flag is on."""
    monkeypatch.setattr(
        radius_population, "active_external_radius_targets", lambda *_a, **_k: []
    )
    with pytest.raises(RuntimeError, match="No DB-configured external RADIUS"):
        radius_population.populate(dry_run=True, source_db=db_session)


def test_the_radius_writer_proceeds_once_the_flag_is_on(db_session, monkeypatch):
    deployed(monkeypatch)
    _flag(db_session, True)
    monkeypatch.setattr(
        radius_population, "active_external_radius_targets", lambda *_a, **_k: []
    )
    # Past the guard: it now fails only for want of a configured target.
    with pytest.raises(RuntimeError, match="No DB-configured external RADIUS"):
        radius_population.populate(dry_run=False, source_db=db_session)


def test_a_worker_process_refuses_to_start_while_the_flag_is_off(
    db_session, monkeypatch, caplog
):
    deployed(monkeypatch)
    from app import celery_app
    from app import db as app_db

    monkeypatch.setattr(app_db, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "rollback", lambda: None)
    monkeypatch.setattr(db_session, "close", lambda: None)

    with pytest.raises(owner.RateOwnerNotSwitchedOn):
        celery_app._assert_rate_owner_switched_on()
    assert "rate_owner_not_switched_on" in {
        getattr(record, "event", None) for record in caplog.records
    }

    _flag(db_session, True)
    celery_app._assert_rate_owner_switched_on()
