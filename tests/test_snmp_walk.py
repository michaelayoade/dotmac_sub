"""Tests for shared SNMP walk helper."""

from __future__ import annotations

from types import SimpleNamespace


def test_run_simple_v2c_walk_uses_snmpbulkwalk_when_available(monkeypatch) -> None:
    from app.services.network import snmp_walk

    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="OID = INTEGER: 1\n", stderr="")

    monkeypatch.setattr(snmp_walk.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(snmp_walk, "decrypt_credential", lambda value: value)
    monkeypatch.setattr(snmp_walk.subprocess, "run", fake_run)

    linked = SimpleNamespace(
        mgmt_ip="192.0.2.10",
        hostname=None,
        snmp_port=None,
        snmp_version="v2c",
        snmp_community="public",
    )

    lines = snmp_walk.run_simple_v2c_walk(linked, ".1.3.6", bulk=True)

    assert lines == ["OID = INTEGER: 1"]
    assert calls["cmd"][0] == "snmpbulkwalk"


def test_run_simple_v2c_walk_falls_back_to_snmpwalk_without_bulk_binary(
    monkeypatch,
) -> None:
    from app.services.network import snmp_walk

    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="OID = INTEGER: 1\n", stderr="")

    monkeypatch.setattr(snmp_walk.shutil, "which", lambda _cmd: None)
    monkeypatch.setattr(snmp_walk, "decrypt_credential", lambda value: value)
    monkeypatch.setattr(snmp_walk.subprocess, "run", fake_run)

    linked = SimpleNamespace(
        mgmt_ip="192.0.2.10",
        hostname=None,
        snmp_port=None,
        snmp_version="v2c",
        snmp_community="public",
    )

    lines = snmp_walk.run_simple_v2c_walk(linked, ".1.3.6", bulk=True)

    assert lines == ["OID = INTEGER: 1"]
    assert calls["cmd"][0] == "snmpwalk"
