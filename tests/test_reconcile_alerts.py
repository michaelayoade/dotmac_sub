"""Tests for the reconciler alert-escalation module.

Covers:
* ZabbixTrapper protocol framing (header + JSON body) via an in-process
  TCP server stub.
* escalate_sweep_unreachable threshold-crossing semantics: only the cycle
  that crosses ``before < threshold <= after`` fires the ERROR log;
  subsequent unreachable cycles log at DEBUG.
* resolve_sweep_unreachable behavior (only fires when ``before > 0``).
* ZabbixTrapper.from_env returns None when ZABBIX_TRAPPER_HOST is unset.
"""

from __future__ import annotations

import json
import logging
import socket
import struct
import threading

from app.services.network.reconcile.alerts import (
    DEFAULT_SWEEP_THRESHOLD,
    SWEEP_ALERT_KIND,
    ZabbixTrapper,
    default_threshold_from_env,
    escalate_sweep_unreachable,
    resolve_sweep_unreachable,
)

# ── ZabbixTrapper protocol ─────────────────────────────────────────────────


class _FakeZabbixServer:
    """Minimal TCP server that speaks the Zabbix trapper protocol.

    Reads one frame (ZBXD header + length + JSON body), records the body,
    responds with ``{"response": "success", "info": ...}``.
    """

    def __init__(self, *, info: str = "processed: 1; failed: 0"):
        self.info = info
        self.received: list[dict] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _FakeZabbixServer:
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            header = self._recv_exact(conn, 13)
            if header is None or not header.startswith(b"ZBXD\x01"):
                return
            length = struct.unpack("<q", header[5:13])[0]
            body = self._recv_exact(conn, length)
            if body is None:
                return
            self.received.append(json.loads(body.decode("utf-8")))

            response_body = json.dumps(
                {"response": "success", "info": self.info}
            ).encode("utf-8")
            frame = b"ZBXD\x01" + struct.pack("<q", len(response_body)) + response_body
            conn.sendall(frame)

    @staticmethod
    def _recv_exact(conn, length):
        buf = bytearray()
        while len(buf) < length:
            chunk = conn.recv(length - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)


def test_zabbix_trapper_sends_well_formed_payload():
    with _FakeZabbixServer() as server:
        trapper = ZabbixTrapper(host="127.0.0.1", port=server.port)
        ok = trapper.send(zabbix_host="172.16.210.20", key="ont.foo", value=3)
    assert ok is True
    assert len(server.received) == 1
    payload = server.received[0]
    assert payload["request"] == "sender data"
    assert payload["data"][0] == {
        "host": "172.16.210.20",
        "key": "ont.foo",
        "value": "3",
    }


def test_zabbix_trapper_returns_false_when_server_rejects_value():
    """Zabbix replies with ``processed: 0`` when the host or trapper key
    isn't configured — treat as failure so a misconfigured item isn't
    silently considered delivered."""
    with _FakeZabbixServer(info="processed: 0; failed: 1") as server:
        trapper = ZabbixTrapper(host="127.0.0.1", port=server.port)
        ok = trapper.send(zabbix_host="x", key="ont.foo", value=3)
    assert ok is False


def test_zabbix_trapper_returns_false_on_connection_error():
    """Network failure (e.g. Zabbix down) is logged and swallowed."""
    trapper = ZabbixTrapper(host="127.0.0.1", port=1, timeout_sec=0.1)
    ok = trapper.send(zabbix_host="x", key="ont.foo", value=3)
    assert ok is False


def test_zabbix_trapper_from_env_unset_returns_none(monkeypatch):
    monkeypatch.delenv("ZABBIX_TRAPPER_HOST", raising=False)
    assert ZabbixTrapper.from_env() is None


def test_zabbix_trapper_from_env_with_host_returns_configured(monkeypatch):
    monkeypatch.setenv("ZABBIX_TRAPPER_HOST", "zabbix.example")
    monkeypatch.setenv("ZABBIX_TRAPPER_PORT", "10052")
    trapper = ZabbixTrapper.from_env()
    assert trapper is not None
    assert trapper.host == "zabbix.example"
    assert trapper.port == 10052


def test_zabbix_trapper_from_env_invalid_port_returns_none(monkeypatch):
    monkeypatch.setenv("ZABBIX_TRAPPER_HOST", "zabbix.example")
    monkeypatch.setenv("ZABBIX_TRAPPER_PORT", "not-a-number")
    assert ZabbixTrapper.from_env() is None


# ── default_threshold_from_env ─────────────────────────────────────────────


def test_default_threshold_from_env_default(monkeypatch):
    monkeypatch.delenv("RECONCILE_SWEEP_ALERT_THRESHOLD", raising=False)
    assert default_threshold_from_env() == DEFAULT_SWEEP_THRESHOLD


def test_default_threshold_from_env_override(monkeypatch):
    monkeypatch.setenv("RECONCILE_SWEEP_ALERT_THRESHOLD", "5")
    assert default_threshold_from_env() == 5


def test_default_threshold_from_env_invalid(monkeypatch):
    monkeypatch.setenv("RECONCILE_SWEEP_ALERT_THRESHOLD", "garbage")
    assert default_threshold_from_env() == DEFAULT_SWEEP_THRESHOLD


def test_default_threshold_from_env_zero_falls_back(monkeypatch):
    """Zero or negative threshold isn't valid; fall back to default rather
    than silently disable alerts."""
    monkeypatch.setenv("RECONCILE_SWEEP_ALERT_THRESHOLD", "0")
    assert default_threshold_from_env() == DEFAULT_SWEEP_THRESHOLD


# ── escalate_sweep_unreachable ─────────────────────────────────────────────


def test_escalate_emits_error_log_on_threshold_crossing(caplog):
    caplog.set_level(logging.ERROR)
    escalate_sweep_unreachable(
        ont_id="ont-1",
        serial_number="HWTC12345678",
        mgmt_ip="172.16.210.20",
        before=2,
        after=3,
        threshold=3,
    )
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    rec = error_records[0]
    assert rec.message == SWEEP_ALERT_KIND
    assert getattr(rec, "alert_action", None) == "escalate"
    assert getattr(rec, "after", None) == 3


def test_escalate_does_not_re_alert_on_subsequent_cycles(caplog):
    """Once past the threshold, subsequent unreachable cycles don't
    re-emit at ERROR (would page the operator twice)."""
    caplog.set_level(logging.DEBUG)
    escalate_sweep_unreachable(
        ont_id="ont-1",
        serial_number="HWTC12345678",
        mgmt_ip="172.16.210.20",
        before=5,
        after=6,
        threshold=3,
    )
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records == []
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(r.message == SWEEP_ALERT_KIND for r in debug_records)


def test_escalate_does_not_alert_before_threshold(caplog):
    """before=0, after=1 with threshold=3 → not crossing yet, log at
    DEBUG only."""
    caplog.set_level(logging.DEBUG)
    escalate_sweep_unreachable(
        ont_id="ont-1",
        serial_number="x",
        mgmt_ip=None,
        before=0,
        after=1,
        threshold=3,
    )
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records == []


def test_escalate_pushes_to_zabbix_when_trapper_configured():
    """Trapper.send is called with the post-increment counter regardless
    of whether the threshold was crossed — keeps Zabbix history fresh so
    the trigger expression can evaluate correctly."""
    calls: list[dict] = []

    class _FakeTrapper:
        def send(self, *, zabbix_host, key, value):
            calls.append({"zabbix_host": zabbix_host, "key": key, "value": value})
            return True

    escalate_sweep_unreachable(
        ont_id="ont-1",
        serial_number="x",
        mgmt_ip="172.16.210.20",
        before=0,
        after=1,
        threshold=3,
        trapper=_FakeTrapper(),
        zabbix_host="172.16.210.20",
    )
    assert len(calls) == 1
    assert calls[0] == {
        "zabbix_host": "172.16.210.20",
        "key": "ont.consecutive_sweep_unreachable",
        "value": 1,
    }


def test_escalate_skips_zabbix_when_no_zabbix_host():
    """zabbix_host=None disables the trapper push even when a trapper
    object is provided — there's no Zabbix host to send to."""
    calls: list[dict] = []

    class _FakeTrapper:
        def send(self, **k):
            calls.append(k)
            return True

    escalate_sweep_unreachable(
        ont_id="ont-1",
        serial_number="x",
        mgmt_ip=None,
        before=2,
        after=3,
        threshold=3,
        trapper=_FakeTrapper(),
        zabbix_host=None,
    )
    assert calls == []


# ── resolve_sweep_unreachable ──────────────────────────────────────────────


def test_resolve_fires_only_when_recovering_from_nonzero_counter(caplog):
    """resolve_sweep_unreachable is a no-op when before == 0 — successful
    reconciles on already-healthy ONTs don't spam recovery alerts."""
    caplog.set_level(logging.INFO)
    resolve_sweep_unreachable(
        ont_id="ont-1",
        serial_number="x",
        mgmt_ip="172.16.210.20",
        before=0,
    )
    assert [r for r in caplog.records if r.levelno == logging.INFO] == []


def test_resolve_emits_info_log_when_recovering(caplog):
    caplog.set_level(logging.INFO)
    resolve_sweep_unreachable(
        ont_id="ont-1",
        serial_number="x",
        mgmt_ip="172.16.210.20",
        before=5,
    )
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    assert getattr(info_records[0], "alert_action", None) == "resolved"


def test_resolve_pushes_zero_to_zabbix_to_clear_trigger():
    calls: list[dict] = []

    class _FakeTrapper:
        def send(self, **k):
            calls.append(k)
            return True

    resolve_sweep_unreachable(
        ont_id="ont-1",
        serial_number="x",
        mgmt_ip="172.16.210.20",
        before=5,
        trapper=_FakeTrapper(),
        zabbix_host="172.16.210.20",
    )
    assert len(calls) == 1
    assert calls[0]["value"] == 0
