from types import SimpleNamespace

from app.services import snmp_probe


def _device(**kwargs):
    values = {
        "mgmt_ip": "192.0.2.10",
        "hostname": None,
        "snmp_port": 161,
        "snmp_version": "2c",
        "snmp_community": "public",
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_probe_snmp_reachability_success(monkeypatch):
    calls = {}
    monkeypatch.setattr(snmp_probe.shutil, "which", lambda _name: "/usr/bin/snmpget")
    monkeypatch.setattr(snmp_probe, "decrypt_credential", lambda value: value)

    def fake_run(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0, stdout="SNMPv2-MIB::sysDescr.0 = x", stderr=""
        )

    monkeypatch.setattr(snmp_probe.subprocess, "run", fake_run)

    result = snmp_probe.probe_snmp_reachability(_device())

    assert result.handled is True
    assert result.success is True
    assert calls["args"][:3] == ["/usr/bin/snmpget", "-v2c", "-c"]
    assert "192.0.2.10:161" in calls["args"]
    assert "1.3.6.1.2.1.1.1.0" in calls["args"]


def test_probe_snmp_reachability_missing_community(monkeypatch):
    monkeypatch.setattr(snmp_probe.shutil, "which", lambda _name: "/usr/bin/snmpget")

    result = snmp_probe.probe_snmp_reachability(_device(snmp_community=None))

    assert result.handled is False
    assert result.success is False
    assert result.error == "missing_snmp_community"


def test_probe_snmp_reachability_failed_response(monkeypatch):
    monkeypatch.setattr(snmp_probe.shutil, "which", lambda _name: "/usr/bin/snmpget")
    monkeypatch.setattr(snmp_probe, "decrypt_credential", lambda value: value)

    def fake_run(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="Timeout")

    monkeypatch.setattr(snmp_probe.subprocess, "run", fake_run)

    result = snmp_probe.probe_snmp_reachability(_device(snmp_version="1"))

    assert result.handled is True
    assert result.success is False
    assert result.error == "Timeout"
