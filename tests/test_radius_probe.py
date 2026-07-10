"""Synthetic RADIUS auth probe: packet build, round trip, health/alert wiring."""

from __future__ import annotations

import hashlib
import hmac
import socket
import struct
import threading

from app.services import admin_alerts, radius_health, radius_probe

_SECRET = b"probe-secret"


def _decode_request(packet: bytes):
    code, pid, length = struct.unpack("!BBH", packet[:4])
    authenticator = packet[4:20]
    attrs = {}
    i = 20
    while i < length:
        atype, alen = struct.unpack("!BB", packet[i : i + 2])
        attrs[atype] = packet[i + 2 : i + alen]
        i += alen
    return code, pid, authenticator, attrs


def test_packet_is_valid_access_request():
    packet, authenticator, pid = radius_probe._build_access_request(
        _SECRET, "sub-health-probe", "pw"
    )
    code, decoded_pid, decoded_auth, attrs = _decode_request(packet)
    assert code == radius_probe._ACCESS_REQUEST
    assert decoded_pid == pid
    assert decoded_auth == authenticator
    assert attrs[radius_probe._ATTR_USER_NAME] == b"sub-health-probe"
    # Message-Authenticator verifies over the packet with the MA field zeroed.
    ma = attrs[radius_probe._ATTR_MESSAGE_AUTHENTICATOR]
    zeroed = packet.replace(ma, b"\x00" * 16)
    expected = hmac.new(_SECRET, zeroed, hashlib.md5).digest()
    assert hmac.compare_digest(expected, ma)


class _FakeServer:
    """Minimal UDP RADIUS responder for the probe round-trip test."""

    def __init__(self, respond_code: int | None):
        self.respond_code = respond_code
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _serve(self):
        try:
            data, addr = self.sock.recvfrom(4096)
        except OSError:
            return
        if self.respond_code is None:
            return  # simulate timeout
        _code, pid, length = struct.unpack("!BBH", data[:4])
        req_auth = data[4:20]
        body = struct.pack("!BBH", self.respond_code, pid, 20)
        resp_auth = hashlib.md5(  # noqa: S324 - RFC 2865 response authenticator
            body + req_auth + _SECRET
        ).digest()
        self.sock.sendto(body + resp_auth, addr)

    def close(self):
        self.sock.close()


def test_probe_accept_measures_rtt():
    server = _FakeServer(radius_probe._ACCESS_ACCEPT).start()
    try:
        result = radius_probe.probe_access_request(
            host="127.0.0.1",
            port=server.port,
            secret=_SECRET.decode(),
            username="sub-health-probe",
            password="pw",
            timeout_seconds=2.0,
        )
    finally:
        server.close()
    assert result.outcome == "accept"
    assert result.responded is True
    assert result.rtt_ms is not None and result.rtt_ms >= 0
    assert result.attempts_used == 1


def test_probe_reject_still_counts_as_responded():
    server = _FakeServer(radius_probe._ACCESS_REJECT).start()
    try:
        result = radius_probe.probe_access_request(
            host="127.0.0.1",
            port=server.port,
            secret=_SECRET.decode(),
            username="sub-health-probe",
            password="pw",
        )
    finally:
        server.close()
    assert result.outcome == "reject"
    assert result.responded is True


def test_probe_timeout_retries_then_reports():
    server = _FakeServer(None).start()  # never answers
    try:
        result = radius_probe.probe_access_request(
            host="127.0.0.1",
            port=server.port,
            secret=_SECRET.decode(),
            username="sub-health-probe",
            password="pw",
            timeout_seconds=0.2,
            attempts=2,
        )
    finally:
        server.close()
    assert result.outcome == "timeout"
    assert result.responded is False
    assert result.attempts_used == 2


def test_unconfigured_probe_is_reported_not_run(monkeypatch):
    monkeypatch.delenv("RADIUS_PROBE_SECRET", raising=False)
    monkeypatch.delenv("RADIUS_PROBE_PASSWORD", raising=False)
    fields, result = radius_probe.run_configured_probe()
    assert fields == {"probe_configured": 0}
    assert result is None


def test_health_gauges_include_probe_when_configured(monkeypatch):
    written = {}

    class _Writer:
        def write_prometheus_lines(self, lines, **kwargs):
            written["lines"] = lines
            return type("R", (), {"success": True, "written": len(lines)})()

    monkeypatch.setattr(radius_health, "_writer", lambda: _Writer())
    radius_health.push_radius_metrics(
        {
            "open_sessions": 800,
            "radacct_read_ok": 1,
            "probe_configured": 1,
            "probe_ok": 1,
            "probe_retries": 0,
            "auth_rtt_ms": 4.2,
        }
    )
    names = {ln.split(" ")[0] for ln in written["lines"]}
    assert "radius_auth_rtt_ms" in names
    assert "radius_probe_ok" in names


def test_probe_gauges_absent_when_unconfigured(monkeypatch):
    written = {}

    class _Writer:
        def write_prometheus_lines(self, lines, **kwargs):
            written["lines"] = lines
            return type("R", (), {"success": True, "written": len(lines)})()

    monkeypatch.setattr(radius_health, "_writer", lambda: _Writer())
    radius_health.push_radius_metrics({"open_sessions": 800, "probe_configured": 0})
    names = {ln.split(" ")[0] for ln in written["lines"]}
    assert "radius_probe_ok" not in names
    assert "radius_auth_rtt_ms" not in names


class _FakeCache:
    def __init__(self):
        self.store = {}

    def get_json(self, key):
        return self.store.get(key)

    def set_json(self, key, value, ttl):
        self.store[key] = value
        return True


def _wire(monkeypatch, result):
    cache = _FakeCache()
    monkeypatch.setattr("app.services.app_cache.get_json", cache.get_json)
    monkeypatch.setattr("app.services.app_cache.set_json", cache.set_json)
    from app.services.task_heartbeat import record_success

    record_success(radius_health.HEARTBEAT_TASK, result)


_BASE = {
    "radacct_read_ok": 1,
    "open_sessions": 800,
    "acct_freshness_seconds": 20.0,
    "suspended_with_session": 0,
}


def test_probe_failure_raises_finding(db_session, monkeypatch):
    _wire(monkeypatch, {**_BASE, "probe_configured": 1, "probe_responded": 0})
    findings = admin_alerts._radius_health_findings(db_session)
    assert "infrastructure:radius:auth-probe-failed" in [
        f.fingerprint for f in findings
    ]


def test_probe_success_no_finding(db_session, monkeypatch):
    _wire(monkeypatch, {**_BASE, "probe_configured": 1, "probe_responded": 1})
    findings = admin_alerts._radius_health_findings(db_session)
    assert "infrastructure:radius:auth-probe-failed" not in [
        f.fingerprint for f in findings
    ]


def test_unconfigured_probe_never_alarms(db_session, monkeypatch):
    _wire(monkeypatch, {**_BASE, "probe_configured": 0, "probe_responded": 0})
    findings = admin_alerts._radius_health_findings(db_session)
    assert "infrastructure:radius:auth-probe-failed" not in [
        f.fingerprint for f in findings
    ]
