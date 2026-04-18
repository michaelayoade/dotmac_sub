"""Zabbix Sender protocol implementation for pushing metrics.

This module provides functions to send custom metrics to Zabbix Server
using the Zabbix Sender protocol. This is useful for:
- Pushing application-level metrics
- Custom monitoring data that doesn't fit standard templates
- Real-time metric updates outside of scheduled polling

The Zabbix Sender protocol is a simple line-based protocol that
Zabbix Server listens on port 10051 (zabbix_trapper items).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Zabbix protocol header
ZABBIX_HEADER = b"ZBXD\x01"
ZABBIX_HEADER_LEN = 13  # 5 bytes header + 8 bytes datalen


@dataclass
class ZabbixMetric:
    """Single metric to send to Zabbix."""

    host: str  # Zabbix host name (must match host in Zabbix)
    key: str  # Zabbix item key
    value: str | int | float  # Metric value
    clock: int | None = None  # Optional Unix timestamp


@dataclass
class ZabbixSenderResponse:
    """Response from Zabbix Server."""

    processed: int
    failed: int
    total: int
    time_spent: float


class ZabbixSenderError(Exception):
    """Error communicating with Zabbix Server."""

    pass


def _get_zabbix_server() -> tuple[str, int]:
    """Get Zabbix Server host and port from environment."""
    host = os.getenv("ZABBIX_SERVER_HOST", "zabbix-server")
    port = int(os.getenv("ZABBIX_SERVER_PORT", "10051"))
    return (host, port)


def _build_request(metrics: list[ZabbixMetric]) -> bytes:
    """Build Zabbix Sender protocol request."""
    data = []
    for metric in metrics:
        item: dict[str, Any] = {
            "host": metric.host,
            "key": metric.key,
            "value": str(metric.value),
        }
        if metric.clock is not None:
            item["clock"] = metric.clock
        data.append(item)

    request = {
        "request": "sender data",
        "data": data,
    }
    json_data = json.dumps(request).encode("utf-8")

    # Build packet: ZBXD\x01 + 8-byte little-endian length + data
    datalen = struct.pack("<Q", len(json_data))
    return ZABBIX_HEADER + datalen + json_data


def _parse_response(data: bytes) -> ZabbixSenderResponse:
    """Parse Zabbix Server response."""
    if len(data) < ZABBIX_HEADER_LEN:
        raise ZabbixSenderError("Invalid response: too short")

    if data[:5] != ZABBIX_HEADER:
        raise ZabbixSenderError("Invalid response: bad header")

    # Extract JSON length
    datalen = struct.unpack("<Q", data[5:13])[0]
    json_data = data[13 : 13 + datalen]

    try:
        response = json.loads(json_data.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ZabbixSenderError(f"Invalid JSON response: {exc}") from exc

    # Parse response info
    info = response.get("info", "")
    # Format: "processed: X; failed: Y; total: Z; seconds spent: S.SS"
    processed = failed = total = 0
    time_spent = 0.0

    parts = info.split(";")
    for part in parts:
        part = part.strip()
        if part.startswith("processed:"):
            processed = int(part.split(":")[1].strip())
        elif part.startswith("failed:"):
            failed = int(part.split(":")[1].strip())
        elif part.startswith("total:"):
            total = int(part.split(":")[1].strip())
        elif part.startswith("seconds spent:"):
            time_spent = float(part.split(":")[1].strip())

    return ZabbixSenderResponse(
        processed=processed,
        failed=failed,
        total=total,
        time_spent=time_spent,
    )


def send_metric(
    host: str,
    key: str,
    value: str | int | float,
    clock: int | None = None,
    timeout: float = 10.0,
) -> bool:
    """Send a single metric to Zabbix.

    Args:
        host: Zabbix host name (must match host in Zabbix)
        key: Zabbix item key (e.g., "app.requests.count")
        value: Metric value
        clock: Optional Unix timestamp (defaults to current time)
        timeout: Socket timeout in seconds

    Returns:
        True if metric was processed successfully, False otherwise.
    """
    result = send_batch([ZabbixMetric(host=host, key=key, value=value, clock=clock)])
    return result.processed > 0


def send_batch(
    metrics: list[ZabbixMetric],
    timeout: float = 10.0,
) -> ZabbixSenderResponse:
    """Send batch of metrics to Zabbix.

    Args:
        metrics: List of ZabbixMetric objects
        timeout: Socket timeout in seconds

    Returns:
        ZabbixSenderResponse with processing statistics.

    Raises:
        ZabbixSenderError: On communication or protocol errors.
    """
    if not metrics:
        return ZabbixSenderResponse(processed=0, failed=0, total=0, time_spent=0.0)

    server_host, server_port = _get_zabbix_server()
    request = _build_request(metrics)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((server_host, server_port))
            sock.sendall(request)

            # Read response
            response_data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response_data += chunk
                # Check if we have complete response
                if len(response_data) >= ZABBIX_HEADER_LEN:
                    expected_len = struct.unpack("<Q", response_data[5:13])[0]
                    if len(response_data) >= ZABBIX_HEADER_LEN + expected_len:
                        break

    except socket.timeout as exc:
        logger.warning(
            "zabbix_sender_timeout",
            extra={"server": f"{server_host}:{server_port}", "metrics_count": len(metrics)},
        )
        raise ZabbixSenderError("Connection timed out") from exc
    except socket.error as exc:
        logger.warning(
            "zabbix_sender_error",
            extra={
                "server": f"{server_host}:{server_port}",
                "metrics_count": len(metrics),
                "error": str(exc),
            },
        )
        raise ZabbixSenderError(f"Socket error: {exc}") from exc

    result = _parse_response(response_data)

    logger.info(
        "zabbix_sender_success",
        extra={
            "processed": result.processed,
            "failed": result.failed,
            "total": result.total,
            "time_spent": result.time_spent,
        },
    )

    return result


def send_metrics_dict(
    host: str,
    metrics: dict[str, str | int | float],
    clock: int | None = None,
    timeout: float = 10.0,
) -> ZabbixSenderResponse:
    """Send multiple metrics for a single host.

    Convenience function to send a dict of key->value pairs.

    Args:
        host: Zabbix host name
        metrics: Dict of {item_key: value}
        clock: Optional Unix timestamp (applied to all metrics)
        timeout: Socket timeout in seconds

    Returns:
        ZabbixSenderResponse with processing statistics.
    """
    metric_list = [
        ZabbixMetric(host=host, key=key, value=value, clock=clock)
        for key, value in metrics.items()
    ]
    return send_batch(metric_list, timeout=timeout)


# Convenience function aliases
push_metric = send_metric
push_batch = send_batch
