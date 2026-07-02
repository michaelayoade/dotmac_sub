"""Tests for SSH-based RouterOS config export and the router_sync helper."""

from __future__ import annotations

import types
from unittest import mock

import pytest

from app.services.router_management import config_export as ce


def _router(ip="10.0.0.1", name="R1"):
    return types.SimpleNamespace(management_ip=ip, name=name)


class _FakeChannel:
    def __init__(self, status=0):
        self._status = status

    def recv_exit_status(self):
        return self._status


class _FakeStd:
    def __init__(self, data=b"", status=0):
        self._data = data
        self.channel = _FakeChannel(status)

    def read(self):
        return self._data


def _patch_client(monkeypatch, stdout=b"", stderr=b"", status=0):
    client = mock.MagicMock()
    client.exec_command.return_value = (
        mock.MagicMock(),
        _FakeStd(stdout, status),
        _FakeStd(stderr, status),
    )
    monkeypatch.setattr(ce.paramiko, "SSHClient", lambda: client)
    monkeypatch.setattr(ce, "_load_private_key", lambda *a, **k: "PKEY")
    return client


def test_export_returns_config_text(monkeypatch):
    client = _patch_client(monkeypatch, stdout=b"/ip address\nadd address=1.1.1.1/32\n")
    out = ce.export_config_via_ssh(
        _router(), username="dotmac-ops", port=120, key_path="/k"
    )
    assert "/ip address" in out
    client.connect.assert_called_once()
    kw = client.connect.call_args.kwargs
    assert kw["hostname"] == "10.0.0.1"
    assert kw["port"] == 120
    assert kw["username"] == "dotmac-ops"
    assert kw["look_for_keys"] is False and kw["allow_agent"] is False
    client.exec_command.assert_called_once_with("/export", timeout=30)
    client.close.assert_called_once()


def test_empty_export_raises(monkeypatch):
    _patch_client(monkeypatch, stdout=b"   ", stderr=b"not enough permissions")
    with pytest.raises(ce.RouterConfigExportError, match="returned no config"):
        ce.export_config_via_ssh(_router(), key_path="/k")


def test_connect_failure_raises_wrapped(monkeypatch):
    client = _patch_client(monkeypatch, stdout=b"x")
    client.connect.side_effect = OSError("no route to host")
    with pytest.raises(ce.RouterConfigExportError, match="failed"):
        ce.export_config_via_ssh(_router(), key_path="/k")
    client.close.assert_called_once()  # closed even on failure


def test_missing_key_path_raises(monkeypatch):
    # settings is a frozen dataclass — swap the whole object for the test.
    monkeypatch.setattr(
        ce,
        "settings",
        types.SimpleNamespace(
            router_config_ssh_username="dotmac-ops",
            router_config_ssh_port=120,
            router_config_ssh_key_path="",
        ),
    )
    with pytest.raises(ce.RouterConfigExportError, match="not configured"):
        ce.export_config_via_ssh(_router(), key_path="")


def test_fetch_config_export_uses_ssh_when_enabled(monkeypatch):
    from app.tasks import router_sync as rs

    monkeypatch.setattr(
        rs, "settings", types.SimpleNamespace(router_config_export_via_ssh=True)
    )
    monkeypatch.setattr(rs, "export_config_via_ssh", lambda router: "SSH-CONFIG")
    # REST path must NOT be called
    monkeypatch.setattr(
        rs.RouterConnectionService,
        "execute",
        mock.Mock(side_effect=AssertionError("REST should not be used")),
    )
    assert rs._fetch_config_export(_router()) == "SSH-CONFIG"


def test_fetch_config_export_rest_fallback_when_disabled(monkeypatch):
    from app.tasks import router_sync as rs

    monkeypatch.setattr(
        rs, "settings", types.SimpleNamespace(router_config_export_via_ssh=False)
    )
    monkeypatch.setattr(
        rs.RouterConnectionService, "execute", lambda *a, **k: ["/ip", "add x"]
    )
    monkeypatch.setattr(
        rs, "export_config_via_ssh", mock.Mock(side_effect=AssertionError("no SSH"))
    )
    assert rs._fetch_config_export(_router()) == "/ip\nadd x"


def test_host_key_policy_tofu_pins_and_persists(monkeypatch, tmp_path):
    client = mock.MagicMock()
    kh = tmp_path / "router_known_hosts"
    monkeypatch.setattr(
        ce,
        "settings",
        types.SimpleNamespace(
            router_config_ssh_known_hosts_path=str(kh),
            router_config_ssh_strict_host_key=False,
        ),
    )
    ce._install_host_key_policy(client)
    # known_hosts is loaded (so a CHANGED key is rejected) + touched for persistence
    client.load_host_keys.assert_called_once_with(str(kh))
    assert kh.exists()
    policy = client.set_missing_host_key_policy.call_args.args[0]
    assert isinstance(policy, ce.paramiko.AutoAddPolicy)


def test_host_key_policy_strict_rejects_unknown(monkeypatch, tmp_path):
    client = mock.MagicMock()
    monkeypatch.setattr(
        ce,
        "settings",
        types.SimpleNamespace(
            router_config_ssh_known_hosts_path=str(tmp_path / "kh"),
            router_config_ssh_strict_host_key=True,
        ),
    )
    ce._install_host_key_policy(client)
    policy = client.set_missing_host_key_policy.call_args.args[0]
    assert isinstance(policy, ce.paramiko.RejectPolicy)
