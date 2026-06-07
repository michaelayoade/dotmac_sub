"""The Zabbix reachability breaker fast-fails after a connection/timeout failure.

A slow or unreachable Zabbix otherwise makes every per-OLT request on the
monitoring dashboard wait out the full HTTP timeout (~28 OLTs → ~100s).
"""

from app.services.zabbix import _ZabbixReachabilityCircuit


def test_reachability_circuit_starts_closed():
    circuit = _ZabbixReachabilityCircuit()
    assert circuit.is_open() is False


def test_reachability_circuit_opens_after_trip():
    circuit = _ZabbixReachabilityCircuit()
    circuit.trip()
    assert circuit.is_open() is True


def test_reachability_circuit_respects_zero_cooldown(monkeypatch):
    # A cooldown floor of 1s is enforced even when configured lower.
    monkeypatch.setenv("ZABBIX_REACHABILITY_CIRCUIT_SECONDS", "1")
    circuit = _ZabbixReachabilityCircuit()
    circuit.trip()
    assert circuit.is_open() is True
