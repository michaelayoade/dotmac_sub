"""Alert escalation for reconcile failure modes.

The sweeper increments ``OntUnit.consecutive_sweep_unreachable`` each cycle
it cannot reach an ONT. After N consecutive cycles (default 3 ≈ 12h on a
4h sweep cadence) the operator needs to know — this module bridges the
in-process counter to whatever monitoring stack is configured.

Two output paths, both best-effort:

* **Structured log line.** Always emitted on threshold crossings. Any log
  aggregator (Promtail, Splunk, etc.) can route on
  ``alert_kind=ont.sweep_unreachable`` plus the per-ONT metadata.
* **Zabbix trapper push.** When ``ZABBIX_TRAPPER_HOST`` is set, send the
  current counter value to Zabbix via the native trapper protocol so the
  Zabbix host's trigger expression can fire (and resolve) on the
  customer-side dashboard. No external binary required.

The Zabbix trapper protocol (TCP/10051, ZBXD header + JSON) is documented
in the Zabbix manual; this module implements it directly so we don't
depend on the ``zabbix_sender`` CLI being installed in the container.

Failure of either path does not propagate — the sweep cycle continues.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── Zabbix trapper protocol ────────────────────────────────────────────────


_ZBX_HEADER = b"ZBXD\x01"
_ZBX_TIMEOUT_SEC = 3.0


@dataclass(frozen=True)
class ZabbixTrapper:
    """Minimal Zabbix trapper sender.

    Configured via env vars on construction; ``from_env`` returns ``None``
    when ``ZABBIX_TRAPPER_HOST`` is unset, which the callers treat as
    "trapper disabled".
    """

    host: str
    port: int = 10051
    timeout_sec: float = _ZBX_TIMEOUT_SEC

    @classmethod
    def from_env(cls) -> ZabbixTrapper | None:
        trapper_host = os.getenv("ZABBIX_TRAPPER_HOST", "").strip()
        if not trapper_host:
            return None
        port_str = os.getenv("ZABBIX_TRAPPER_PORT", "10051").strip() or "10051"
        try:
            port = int(port_str)
        except ValueError:
            logger.warning("zabbix_trapper_port_invalid", extra={"value": port_str})
            return None
        return cls(host=trapper_host, port=port)

    def send(self, *, zabbix_host: str, key: str, value: object) -> bool:
        """Push one trap to Zabbix. Returns True on a server-accepted
        response. Network errors / malformed replies log a warning and
        return False — the caller keeps going."""
        payload = {
            "request": "sender data",
            "data": [
                {
                    "host": zabbix_host,
                    "key": key,
                    "value": str(value),
                }
            ],
            "clock": int(time.time()),
        }
        body = json.dumps(payload).encode("utf-8")
        frame = _ZBX_HEADER + struct.pack("<q", len(body)) + body

        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout_sec
            ) as sock:
                sock.sendall(frame)
                response = _read_zbx_response(sock)
        except OSError as exc:
            logger.warning(
                "zabbix_trapper_send_failed",
                extra={
                    "trapper_host": self.host,
                    "zabbix_host": zabbix_host,
                    "key": key,
                    "error": str(exc),
                },
            )
            return False

        if response is None:
            return False
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError:
            logger.warning(
                "zabbix_trapper_response_malformed",
                extra={"response_bytes": response[:120]},
            )
            return False
        info = str(parsed.get("info", ""))
        # Zabbix returns "processed: N; failed: M; ...". Treat any
        # processed-zero as failure so misconfigured items don't masquerade
        # as success.
        if "processed: 0" in info:
            logger.warning(
                "zabbix_trapper_value_rejected",
                extra={
                    "zabbix_host": zabbix_host,
                    "key": key,
                    "info": info,
                },
            )
            return False
        return parsed.get("response") == "success"


def _read_zbx_response(sock: socket.socket) -> bytes | None:
    """Read a Zabbix protocol response. Returns the JSON body bytes or
    None on malformed framing."""
    header = _recv_exact(sock, 13)
    if header is None or not header.startswith(_ZBX_HEADER):
        return None
    length = struct.unpack("<q", header[5:13])[0]
    if length <= 0 or length > 1_000_000:
        return None
    return _recv_exact(sock, length)


def _recv_exact(sock: socket.socket, length: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < length:
        chunk = sock.recv(length - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


# ── Sweep-unreachable alert escalation ─────────────────────────────────────


DEFAULT_SWEEP_THRESHOLD = 3
SWEEP_ALERT_KIND = "ont.sweep_unreachable"
SWEEP_TRAPPER_KEY_DEFAULT = "ont.consecutive_sweep_unreachable"


def default_threshold_from_env() -> int:
    """``RECONCILE_SWEEP_ALERT_THRESHOLD`` env override, default 3."""
    raw = os.getenv("RECONCILE_SWEEP_ALERT_THRESHOLD", "").strip()
    if not raw:
        return DEFAULT_SWEEP_THRESHOLD
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "reconcile_sweep_alert_threshold_invalid",
            extra={"value": raw},
        )
        return DEFAULT_SWEEP_THRESHOLD
    return value if value > 0 else DEFAULT_SWEEP_THRESHOLD


def trapper_key_from_env() -> str:
    return (
        os.getenv("ZABBIX_TRAPPER_SWEEP_KEY", "").strip() or SWEEP_TRAPPER_KEY_DEFAULT
    )


def escalate_sweep_unreachable(
    *,
    ont_id: str,
    serial_number: str,
    mgmt_ip: str | None,
    before: int,
    after: int,
    threshold: int = DEFAULT_SWEEP_THRESHOLD,
    trapper: ZabbixTrapper | None = None,
    zabbix_host: str | None = None,
    trapper_key: str | None = None,
) -> None:
    """Called by the sweeper after incrementing ``after``.

    Always pushes the current counter value to Zabbix (so the trigger
    expression has fresh data) when a trapper is configured. Emits a
    structured ERROR log only on threshold crossings (``before < threshold
    <= after``) to avoid alert-fatigue per-sweep.

    Resolution is handled by ``resolve_sweep_unreachable``, called from
    the success path in core.py.
    """
    crossing = before < threshold <= after
    log_level = logging.ERROR if crossing else logging.DEBUG
    logger.log(
        log_level,
        SWEEP_ALERT_KIND,
        extra={
            "alert_kind": SWEEP_ALERT_KIND,
            "alert_action": "escalate" if crossing else "still_unreachable",
            "ont_id": str(ont_id),
            "serial_number": serial_number,
            "mgmt_ip": mgmt_ip,
            "before": before,
            "after": after,
            "threshold": threshold,
        },
    )

    if trapper is None or not zabbix_host:
        return

    trapper.send(
        zabbix_host=zabbix_host,
        key=trapper_key or trapper_key_from_env(),
        value=after,
    )


def resolve_sweep_unreachable(
    *,
    ont_id: str,
    serial_number: str,
    mgmt_ip: str | None,
    before: int,
    trapper: ZabbixTrapper | None = None,
    zabbix_host: str | None = None,
    trapper_key: str | None = None,
) -> None:
    """Called from the reconcile core when the counter resets from a
    non-zero value to zero. Emits a single INFO ``resolved`` log and pushes
    value=0 to Zabbix so the trigger clears."""
    if before <= 0:
        return

    logger.info(
        SWEEP_ALERT_KIND,
        extra={
            "alert_kind": SWEEP_ALERT_KIND,
            "alert_action": "resolved",
            "ont_id": str(ont_id),
            "serial_number": serial_number,
            "mgmt_ip": mgmt_ip,
            "before": before,
            "after": 0,
        },
    )

    if trapper is None or not zabbix_host:
        return

    trapper.send(
        zabbix_host=zabbix_host,
        key=trapper_key or trapper_key_from_env(),
        value=0,
    )


__all__ = (
    "DEFAULT_SWEEP_THRESHOLD",
    "ZabbixTrapper",
    "default_threshold_from_env",
    "escalate_sweep_unreachable",
    "resolve_sweep_unreachable",
    "trapper_key_from_env",
)
