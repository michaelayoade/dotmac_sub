"""Tests for the mgmt-IP reachability helper."""

from __future__ import annotations

from app.services.network.reconcile.readers.reachability import is_pingable


def test_is_pingable_returns_false_for_none_ip():
    assert is_pingable(None) is False


def test_is_pingable_returns_false_for_empty_ip():
    assert is_pingable("") is False


def test_is_pingable_uses_injected_ping_function():
    calls: list = []

    def _fake_ping(ip, count, timeout_sec):
        calls.append((ip, count, timeout_sec))
        return True

    result = is_pingable(
        "172.16.210.20", count=3, timeout_sec=1.0, ping_function=_fake_ping
    )
    assert result is True
    assert calls == [("172.16.210.20", 3, 1.0)]


def test_is_pingable_handles_exception_from_ping_function():
    """A misbehaving ping function shouldn't crash the reconciler — return
    False as the safe default."""

    def _explode(ip, count, timeout_sec):
        raise RuntimeError("kaboom")

    assert is_pingable("1.2.3.4", ping_function=_explode) is False


def test_is_pingable_false_signal_for_unreachable_ip():
    """Stub the ping function to mimic an unreachable host."""

    def _no_reply(ip, count, timeout_sec):
        return False

    assert is_pingable("1.2.3.4", ping_function=_no_reply) is False
