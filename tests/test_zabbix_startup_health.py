"""The startup Zabbix health probe retries before warning, so a transient
failure during the saturated post-seed window doesn't false-alarm. It must
also never raise into the startup path."""

from __future__ import annotations

import asyncio

import app.main as main
import app.services.zabbix as zabbix_service


def _run(coro):
    return asyncio.run(coro)


def test_retries_until_available(monkeypatch):
    monkeypatch.setattr(main, "_ZABBIX_STARTUP_HEALTH_ATTEMPTS", 3)
    monkeypatch.setattr(main, "_ZABBIX_STARTUP_HEALTH_RETRY_DELAY", 0)

    calls = {"n": 0}

    def fake_check(timeout=10.0):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"available": False, "status": "unavailable", "configured": True}
        return {"available": True, "status": "up", "configured": True}

    monkeypatch.setattr(zabbix_service, "check_zabbix_availability", fake_check)

    _run(main._log_zabbix_startup_health())
    # Stops retrying as soon as it sees availability.
    assert calls["n"] == 3


def test_exhausts_attempts_then_warns(monkeypatch, caplog):
    monkeypatch.setattr(main, "_ZABBIX_STARTUP_HEALTH_ATTEMPTS", 2)
    monkeypatch.setattr(main, "_ZABBIX_STARTUP_HEALTH_RETRY_DELAY", 0)

    calls = {"n": 0}

    def fake_check(timeout=10.0):
        calls["n"] += 1
        return {
            "available": False,
            "status": "unavailable",
            "configured": True,
            "message": "boom",
        }

    monkeypatch.setattr(zabbix_service, "check_zabbix_availability", fake_check)

    with caplog.at_level("WARNING"):
        _run(main._log_zabbix_startup_health())

    assert calls["n"] == 2  # used all attempts before giving up
    assert any(r.message == "zabbix_startup_health" for r in caplog.records)


def test_exception_does_not_propagate(monkeypatch):
    """A probe that raises must be swallowed (retried), never bubbled into
    the startup task."""
    monkeypatch.setattr(main, "_ZABBIX_STARTUP_HEALTH_ATTEMPTS", 2)
    monkeypatch.setattr(main, "_ZABBIX_STARTUP_HEALTH_RETRY_DELAY", 0)

    def boom(timeout=10.0):
        raise RuntimeError("zabbix client blew up")

    monkeypatch.setattr(zabbix_service, "check_zabbix_availability", boom)

    # Should complete without raising.
    _run(main._log_zabbix_startup_health())
