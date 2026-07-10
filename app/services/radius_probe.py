"""Synthetic RADIUS auth probe (stdlib RFC 2865 client).

Sends a real ``Access-Request`` for a dedicated probe credential and measures
the round trip — the same path a customer's PPPoE auth takes (UDP in,
FreeRADIUS, SQL authorize, response out). This is the customer-experience
signal the DB-derived health pass can't see: accounting can look fresh while
new auth attempts time out.

Deliberately dependency-free: RADIUS packets are ~40 lines of hashlib. The
probe client must be registered with FreeRADIUS (a ``nas``-table row covering
the worker subnet — ``read_clients = yes`` loads it at startup) and the probe
user must exist in ``radcheck`` (``radius_population`` exempts it from orphan
cleanup by name).

Config is env-only (secrets never live in DB settings):

- ``RADIUS_PROBE_SECRET`` / ``RADIUS_PROBE_SECRET_FILE`` — client secret
- ``RADIUS_PROBE_PASSWORD`` / ``RADIUS_PROBE_PASSWORD_FILE`` — probe user's
  cleartext password
- ``RADIUS_PROBE_USERNAME`` (default ``sub-health-probe``)
- ``RADIUS_PROBE_HOST`` (default ``freeradius``) / ``RADIUS_PROBE_PORT`` (1812)

An unset secret/password disables the probe (reported, never alarming).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets as _secrets
import socket
import struct
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_ACCESS_REQUEST = 1
_ACCESS_ACCEPT = 2
_ACCESS_REJECT = 3

_ATTR_USER_NAME = 1
_ATTR_USER_PASSWORD = 2
_ATTR_NAS_IDENTIFIER = 32
_ATTR_MESSAGE_AUTHENTICATOR = 80

DEFAULT_USERNAME = "sub-health-probe"
DEFAULT_HOST = "freeradius"
DEFAULT_PORT = 1812
DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_ATTEMPTS = 2

_NAS_IDENTIFIER = b"dotmac-sub-health-probe"


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one probe: accept/reject prove the auth path answered."""

    outcome: str  # "accept" | "reject" | "timeout" | "error"
    rtt_ms: float | None
    attempts_used: int

    @property
    def responded(self) -> bool:
        return self.outcome in ("accept", "reject")


def _read_secret_file(path: str | None) -> str:
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _env_or_file(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    return _read_secret_file(os.getenv(f"{name}_FILE"))


def probe_username() -> str:
    return os.getenv("RADIUS_PROBE_USERNAME", DEFAULT_USERNAME).strip()


def probe_config() -> dict:
    """Resolved probe configuration; ``configured`` gates everything."""
    secret = _env_or_file("RADIUS_PROBE_SECRET")
    password = _env_or_file("RADIUS_PROBE_PASSWORD")
    try:
        port = int(os.getenv("RADIUS_PROBE_PORT", str(DEFAULT_PORT)))
    except ValueError:
        port = DEFAULT_PORT
    return {
        "configured": bool(secret and password),
        "host": os.getenv("RADIUS_PROBE_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST,
        "port": port,
        "secret": secret,
        "username": probe_username(),
        "password": password,
    }


def _encrypt_password(secret: bytes, authenticator: bytes, password: bytes) -> bytes:
    """RFC 2865 §5.2 User-Password obfuscation (md5 xor chain)."""
    padded = password + b"\x00" * (-len(password) % 16)
    out = b""
    previous = authenticator
    for i in range(0, len(padded), 16):
        digest = hashlib.md5(  # noqa: S324 - RFC 2865 obfuscation, not a security hash
            secret + previous, usedforsecurity=False
        ).digest()
        block = bytes(a ^ b for a, b in zip(padded[i : i + 16], digest))
        out += block
        previous = block
    return out


def _attr(attr_type: int, value: bytes) -> bytes:
    return struct.pack("!BB", attr_type, len(value) + 2) + value


def _build_access_request(
    secret: bytes, username: str, password: str
) -> tuple[bytes, bytes, int]:
    """Returns (packet, request_authenticator, packet_id)."""
    packet_id = _secrets.randbelow(256)
    authenticator = _secrets.token_bytes(16)
    attrs = _attr(_ATTR_USER_NAME, username.encode())
    attrs += _attr(
        _ATTR_USER_PASSWORD, _encrypt_password(secret, authenticator, password.encode())
    )
    attrs += _attr(_ATTR_NAS_IDENTIFIER, _NAS_IDENTIFIER)
    # Message-Authenticator (RFC 3579): computed over the packet with the MA
    # attribute zeroed. Include it so the server can be configured to require
    # authenticated Access-Requests.
    attrs_with_ma = attrs + _attr(_ATTR_MESSAGE_AUTHENTICATOR, b"\x00" * 16)
    length = 20 + len(attrs_with_ma)
    header = struct.pack("!BBH", _ACCESS_REQUEST, packet_id, length) + authenticator
    ma = hmac.new(secret, header + attrs_with_ma, hashlib.md5).digest()
    packet = header + attrs + _attr(_ATTR_MESSAGE_AUTHENTICATOR, ma)
    return packet, authenticator, packet_id


def _response_valid(
    data: bytes, secret: bytes, request_authenticator: bytes, packet_id: int
) -> int | None:
    """Validate a response; returns its code or None when it doesn't verify."""
    if len(data) < 20:
        return None
    code, resp_id, length = struct.unpack("!BBH", data[:4])
    if resp_id != packet_id or length > len(data):
        return None
    expected = hashlib.md5(  # noqa: S324 - RFC 2865 response authenticator
        data[:4] + request_authenticator + data[20:length] + secret,
        usedforsecurity=False,
    ).digest()
    if not hmac.compare_digest(expected, data[4:20]):
        return None
    return code


def probe_access_request(
    *,
    host: str,
    port: int,
    secret: str,
    username: str,
    password: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = DEFAULT_ATTEMPTS,
) -> ProbeResult:
    """Send an Access-Request, retrying on timeout; measure the round trip."""
    secret_b = secret.encode()
    packet, authenticator, packet_id = _build_access_request(
        secret_b, username, password
    )
    attempts = max(1, attempts)
    used = 0
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout_seconds)
            for _ in range(attempts):
                used += 1
                started = time.monotonic()
                sock.sendto(packet, (host, port))
                try:
                    while True:
                        data, _addr = sock.recvfrom(4096)
                        code = _response_valid(data, secret_b, authenticator, packet_id)
                        if code == _ACCESS_ACCEPT:
                            return ProbeResult(
                                "accept",
                                (time.monotonic() - started) * 1000.0,
                                used,
                            )
                        if code == _ACCESS_REJECT:
                            return ProbeResult(
                                "reject",
                                (time.monotonic() - started) * 1000.0,
                                used,
                            )
                        # unverifiable datagram — keep listening this attempt
                except TimeoutError:
                    continue
        return ProbeResult("timeout", None, used)
    except OSError as exc:
        logger.warning("radius_probe_socket_error: %s", exc)
        return ProbeResult("error", None, used)


def run_configured_probe() -> tuple[dict, ProbeResult | None]:
    """Probe using env config. Returns ``(numeric_health_fields, result)``.

    The fields merge straight into the radius-health snapshot: everything is
    numeric so the task heartbeat keeps them.
    """
    config = probe_config()
    if not config["configured"]:
        return {"probe_configured": 0}, None
    result = probe_access_request(
        host=config["host"],
        port=config["port"],
        secret=config["secret"],
        username=config["username"],
        password=config["password"],
    )
    fields: dict = {
        "probe_configured": 1,
        "probe_ok": 1 if result.outcome == "accept" else 0,
        "probe_responded": 1 if result.responded else 0,
        "probe_attempts": result.attempts_used,
        "probe_retries": max(0, result.attempts_used - 1),
    }
    if result.rtt_ms is not None:
        fields["auth_rtt_ms"] = round(result.rtt_ms, 2)
    return fields, result
