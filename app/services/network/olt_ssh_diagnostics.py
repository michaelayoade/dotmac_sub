"""Read-only Huawei OLT diagnostic commands and parsers."""

from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
from app.services.network.olt_validators import validate_fsp, validate_ont_id
from app.services.network.parsers.loader import OntInfoEntry, parse_ont_info_detail

logger = logging.getLogger(__name__)

_SSH_CONNECTION_ERRORS = (
    SSHException,
    OSError,
    socket.timeout,
    TimeoutError,
    ConnectionError,
)


@dataclass(frozen=True)
class AlarmEntry:
    """A normalized active OLT alarm row."""

    alarm_id: int | None
    severity: str
    source: str
    name: str
    raised_at: datetime | None = None
    sequence: int | None = None
    raw: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OntTrafficStats:
    """Read-only ONT traffic counters parsed from Huawei CLI output."""

    fsp: str
    ont_id: int
    upstream_bytes: int | None = None
    downstream_bytes: int | None = None
    upstream_packets: int | None = None
    downstream_packets: int | None = None
    upstream_rate_kbps: float | None = None
    downstream_rate_kbps: float | None = None
    raw: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OpticalInfo:
    """Optical/DDM values from ``display ont optical-info``."""

    fsp: str
    ont_id: int
    rx_power_dbm: float | None = None
    tx_power_dbm: float | None = None
    olt_rx_power_dbm: float | None = None
    temperature_c: float | None = None
    voltage_v: float | None = None
    bias_current_ma: float | None = None
    raw: dict[str, str] = field(default_factory=dict)


_KV_RE = re.compile(r"^\s*(?P<key>[^:：=]+?)\s*[:：=]\s*(?P<value>.*?)\s*$")
_ALARM_ID_RE = re.compile(r"(?:alarm[- ]?id|alarm id)\D*(?P<id>0x[0-9a-f]+|\d+)", re.I)
_SEVERITY_VALUES = {"critical", "major", "minor", "warning", "event"}


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        match = re.search(r"0x[0-9a-f]+|\d+", text, re.I)
        if not match:
            return None
        token = match.group(0)
        try:
            return int(token, 16) if token.lower().startswith("0x") else int(token)
        except ValueError:
            return None


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def _parse_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(
        r"\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}(?::\d{2})?",
        text,
    )
    candidates = [text]
    if match:
        candidates.insert(0, match.group(0))
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
    ):
        for candidate in candidates:
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


def _normalize_severity(value: str | None) -> str:
    text = str(value or "").strip().lower()
    for severity in _SEVERITY_VALUES:
        if severity in text:
            return severity
    return text or "unknown"


def _parse_kv_blocks(output: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        match = _KV_RE.match(line)
        if not match:
            if not line.strip() and current:
                blocks.append(current)
                current = {}
            continue
        key = _normalize_key(match.group("key"))
        value = match.group("value").strip()
        current_keys = set(current)
        if (
            key in {"alarm_id", "sequence", "alarm_serial_number"}
            and current
            and not (key == "alarm_id" and current_keys <= {"sequence"})
        ):
            blocks.append(current)
            current = {}
        current[key] = value
    if current:
        blocks.append(current)
    return blocks


def _alarm_from_kv(block: dict[str, str]) -> AlarmEntry | None:
    alarm_id = _parse_int(
        block.get("alarm_id")
        or block.get("alarm_id_hex")
        or block.get("event_id")
        or block.get("id")
    )
    sequence = _parse_int(
        block.get("sequence") or block.get("alarm_serial_number") or block.get("sn")
    )
    severity = _normalize_severity(
        block.get("severity") or block.get("level") or block.get("alarm_level")
    )
    source = (
        block.get("source")
        or block.get("location_info")
        or block.get("position")
        or block.get("location")
        or ""
    )
    name = (
        block.get("alarm_name")
        or block.get("name")
        or block.get("alarm_cause")
        or block.get("description")
        or ""
    )
    raised_at = _parse_datetime(
        block.get("alarm_raised_time")
        or block.get("raise_time")
        or block.get("occur_time")
        or block.get("time")
    )
    if alarm_id is None and not name and not source:
        return None
    return AlarmEntry(
        alarm_id=alarm_id,
        severity=severity,
        source=source,
        name=name,
        raised_at=raised_at,
        sequence=sequence,
        raw=dict(block),
    )


def parse_active_alarms(output: str) -> list[AlarmEntry]:
    """Parse Huawei ``display alarm active all`` output."""
    alarms = [_alarm_from_kv(block) for block in _parse_kv_blocks(output)]
    parsed = [alarm for alarm in alarms if alarm is not None]
    if parsed:
        return parsed

    entries: list[AlarmEntry] = []
    for line in output.splitlines():
        text = line.strip()
        if not text or text.startswith(("-", "=")):
            continue
        lowered = text.lower()
        if "alarm" in lowered and ("severity" in lowered or "name" in lowered):
            continue
        severity = next((item for item in _SEVERITY_VALUES if item in lowered), "")
        if not severity:
            continue
        alarm_id_match = _ALARM_ID_RE.search(text)
        alarm_id = _parse_int(alarm_id_match.group("id")) if alarm_id_match else None
        raised_at_match = re.search(
            r"\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}(?::\d{2})?",
            text,
        )
        raised_at = _parse_datetime(raised_at_match.group(0) if raised_at_match else "")
        entries.append(
            AlarmEntry(
                alarm_id=alarm_id,
                severity=severity,
                source="",
                name=text,
                raised_at=raised_at,
                raw={"line": text},
            )
        )
    return entries


def parse_ont_traffic(output: str, *, fsp: str, ont_id: int) -> OntTrafficStats:
    """Parse Huawei ``display ont traffic`` output for one ONT."""
    raw: dict[str, str] = {}
    for line in output.splitlines():
        match = _KV_RE.match(line)
        if not match:
            continue
        raw[_normalize_key(match.group("key"))] = match.group("value").strip()

    def first(*keys: str) -> str | None:
        for key in keys:
            if key in raw:
                return raw[key]
        return None

    return OntTrafficStats(
        fsp=fsp,
        ont_id=ont_id,
        upstream_bytes=_parse_int(
            first("upstream_bytes", "up_bytes", "upstream_byte", "up_byte")
        ),
        downstream_bytes=_parse_int(
            first(
                "downstream_bytes",
                "down_bytes",
                "downstream_byte",
                "down_byte",
            )
        ),
        upstream_packets=_parse_int(
            first("upstream_packets", "up_packets", "upstream_packet", "up_packet")
        ),
        downstream_packets=_parse_int(
            first(
                "downstream_packets",
                "down_packets",
                "downstream_packet",
                "down_packet",
            )
        ),
        upstream_rate_kbps=_parse_float(
            first("upstream_rate_kbps", "upstream_rate", "up_rate")
        ),
        downstream_rate_kbps=_parse_float(
            first("downstream_rate_kbps", "downstream_rate", "down_rate")
        ),
        raw=raw,
    )


def parse_ont_optical_info(output: str, *, fsp: str, ont_id: int) -> OpticalInfo:
    """Parse Huawei ``display ont optical-info`` output for one ONT."""
    raw: dict[str, str] = {}
    for line in output.splitlines():
        match = _KV_RE.match(line)
        if not match:
            continue
        raw[_normalize_key(match.group("key"))] = match.group("value").strip()

    def first(*keys: str) -> str | None:
        for key in keys:
            if key in raw:
                return raw[key]
        return None

    return OpticalInfo(
        fsp=fsp,
        ont_id=ont_id,
        rx_power_dbm=_parse_float(
            first(
                "rx_optical_power_dbm",
                "rx_power_dbm",
                "receive_optical_power_dbm",
                "ont_rx_power_dbm",
                "ont_rx_optical_power_dbm",
            )
        ),
        tx_power_dbm=_parse_float(
            first(
                "tx_optical_power_dbm",
                "tx_power_dbm",
                "transmit_optical_power_dbm",
                "ont_tx_power_dbm",
                "ont_tx_optical_power_dbm",
            )
        ),
        olt_rx_power_dbm=_parse_float(
            first(
                "olt_rx_ont_optical_power_dbm",
                "olt_rx_optical_power_dbm",
                "olt_rx_power_dbm",
            )
        ),
        temperature_c=_parse_float(
            first(
                "temperature_c",
                "temperature",
                "laser_temperature_c",
                "ont_temperature_c",
            )
        ),
        voltage_v=_parse_float(first("voltage_v", "voltage", "supply_voltage_v")),
        bias_current_ma=_parse_float(
            first(
                "bias_current_ma",
                "bias_current",
                "laser_bias_current_ma",
                "laser_current_ma",
            )
        ),
        raw=raw,
    )


def _run_readonly_command(olt: OLTDevice, command: str) -> tuple[bool, str, str]:
    from app.services.network.olt_ssh import (
        _open_shell,
        _read_until_prompt,
        _run_huawei_cmd,
    )

    try:
        transport, channel, _policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", ""

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        return True, "ok", _run_huawei_cmd(channel, command)
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error running diagnostic command on OLT %s: %s",
            getattr(olt, "name", "?"),
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}", ""
    finally:
        transport.close()


def get_active_alarms(olt: OLTDevice) -> tuple[bool, str, list[AlarmEntry]]:
    """Query active alarms from an OLT."""
    ok, message, output = _run_readonly_command(olt, "display alarm active all")
    if not ok:
        return False, message, []
    entries = parse_active_alarms(output)
    return True, f"Found {len(entries)} active alarm(s)", entries


def get_ont_info(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
) -> tuple[bool, str, OntInfoEntry | None]:
    """Query detailed ONT info from an OLT."""
    try:
        clean_fsp = validate_fsp(fsp)
        clean_ont_id = validate_ont_id(ont_id)
        from app.services.network.huawei_command_profiles import (
            get_huawei_command_profile,
        )

        command = get_huawei_command_profile(olt).display_ont_info(
            clean_fsp,
            clean_ont_id,
        )
    except Exception as exc:
        return False, str(exc), None

    ok, message, output = _run_readonly_command(olt, command)
    if not ok:
        return False, message, None
    info = parse_ont_info_detail(output)
    if info is None:
        return False, "ONT info output could not be parsed", None
    return True, "ONT info read", info


def get_ont_traffic_stats(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
) -> tuple[bool, str, OntTrafficStats | None]:
    """Query traffic counters for one ONT."""
    try:
        clean_fsp = validate_fsp(fsp)
        clean_ont_id = validate_ont_id(ont_id)
    except Exception as exc:
        return False, str(exc), None

    command = f"display ont traffic {clean_fsp} {clean_ont_id}"
    ok, message, output = _run_readonly_command(olt, command)
    if not ok:
        return False, message, None
    stats = parse_ont_traffic(output, fsp=clean_fsp, ont_id=clean_ont_id)
    return True, "ONT traffic stats read", stats


def get_ont_optical_info(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
) -> tuple[bool, str, OpticalInfo | None]:
    """Query optical/DDM values for one ONT."""
    try:
        clean_fsp = validate_fsp(fsp)
        clean_ont_id = validate_ont_id(ont_id)
        from app.services.network.huawei_command_profiles import (
            get_huawei_command_profile,
        )

        command = get_huawei_command_profile(olt).display_ont_optical_info(
            clean_fsp,
            clean_ont_id,
        )
    except Exception as exc:
        return False, str(exc), None

    ok, message, output = _run_readonly_command(olt, command)
    if not ok:
        return False, message, None
    info = parse_ont_optical_info(output, fsp=clean_fsp, ont_id=clean_ont_id)
    return True, "ONT optical info read", info
