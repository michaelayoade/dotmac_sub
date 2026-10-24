"""Behaviour of the environment-only outbound-effect gate."""

from __future__ import annotations

import dataclasses
import logging

import pytest

from app.config import settings as base_settings
from app.services import outbound_effects as oe
from app.services.outbound_effects import Effect, OutboundEffectBlocked


def _use(monkeypatch: pytest.MonkeyPatch, app_env: str, allow: str = "") -> None:
    monkeypatch.setattr(
        oe,
        "settings",
        dataclasses.replace(
            base_settings, app_env=app_env, outbound_effects_allow=allow
        ),
    )


@pytest.mark.parametrize("env", ["production", "prod", "Production"])
def test_production_permits_everything(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    _use(monkeypatch, env)
    for effect in Effect:
        oe.require(effect)


@pytest.mark.parametrize("env", ["staging", "development", "test", "", "unknown"])
def test_non_production_blocks_by_default(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    _use(monkeypatch, env)
    for effect in Effect:
        with pytest.raises(OutboundEffectBlocked) as caught:
            oe.require(effect)
        assert caught.value.effect is effect


def test_allowlist_permits_only_named_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, "staging", " email , SMS ")
    oe.require(Effect.email)
    oe.require(Effect.sms)
    with pytest.raises(OutboundEffectBlocked):
        oe.require(Effect.radius_coa)
    assert oe.status()["allowed"] == ["email", "sms"]


def test_unknown_allowlist_entries_permit_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, "staging", "everything,*,all,radius_coa_typo")
    assert oe.allowed_effects() == frozenset()
    with pytest.raises(OutboundEffectBlocked):
        oe.require(Effect.radius_coa)


def test_block_log_names_effect_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _use(monkeypatch, "staging")
    with caplog.at_level(logging.WARNING, logger=oe.logger.name):
        with pytest.raises(OutboundEffectBlocked):
            oe.require(Effect.router_api)
    assert "effect=router_api" in caplog.text


def test_database_value_cannot_enable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate has no database path, so a copied prod row cannot open it."""
    import inspect

    source = inspect.getsource(oe)
    for needle in ("Session", "sqlalchemy", "control_registry", "DomainSetting"):
        assert needle not in source
    _use(monkeypatch, "staging")
    with pytest.raises(OutboundEffectBlocked):
        oe.require(Effect.radius_coa)


def test_chokepoint_blocks_before_opening_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked CoA exchange raises before any socket would be created."""
    from app.services import enforcement

    class Boom:
        def __init__(self, *a: object, **k: object) -> None:
            raise AssertionError("transport opened despite block")

    monkeypatch.setattr(enforcement, "Client", Boom)
    _use(monkeypatch, "staging")
    with pytest.raises(OutboundEffectBlocked):
        enforcement._exchange_coa_disconnect(
            host="192.0.2.1",
            secret=b"x",
            dictionary=object(),  # type: ignore[arg-type]
            port=3799,
            retries=1,
            timeout=1,
            username="probe",
            framed_ip=None,
            session_id=None,
            log_ref="t",
        )
