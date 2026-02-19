from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass

from app.models.network_monitoring import (
    DeviceInterface,
    InterfaceStatus,
    NetworkDevice,
)

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class InterfaceSnapshot:
    index: str
    name: str
    description: str | None
    status: InterfaceStatus
    speed_mbps: int | None


def _snmpwalk_args(device: NetworkDevice, command: str = "snmpwalk", bulk: bool = False) -> list[str]:
    host = device.mgmt_ip or device.hostname
    if not host:
        raise ValueError("Missing management IP/hostname for SNMP walk.")

    version = (device.snmp_version or "v2c").lower()
    args = [command, "-t", "5", "-r", "2", "-m", ""]
    if bulk:
        args.append("-Cr25")
    if version in {"2c", "v2c"}:
        community = device.snmp_community or "public"
        args += ["-v2c", "-c", community]
    elif version == "v3":
        username = device.snmp_username
        if not username:
            raise ValueError("SNMP v3 requires a username.")

        auth_secret = device.snmp_auth_secret
        priv_secret = device.snmp_priv_secret
        auth_protocol = (device.snmp_auth_protocol or "none").lower()
        priv_protocol = (device.snmp_priv_protocol or "none").lower()

        if auth_secret and priv_secret:
            level = "authPriv"
        elif auth_secret:
            level = "authNoPriv"
        else:
            level = "noAuthNoPriv"

        args += ["-v3", "-l", level, "-u", username]
        if auth_secret and auth_protocol != "none":
            args += ["-a", auth_protocol.upper(), "-A", auth_secret]
        if priv_secret and priv_protocol != "none":
            args += ["-x", priv_protocol.upper(), "-X", priv_secret]
    else:
        raise ValueError(f"Unsupported SNMP version: {device.snmp_version}")

    if device.snmp_port:
        host = f"{host}:{device.snmp_port}"

    args.append(host)
    return args


def _run_snmpwalk(device: NetworkDevice, oid: str, timeout: int = 20) -> list[str]:
    args = _snmpwalk_args(device) + [oid]
    return _run_snmp_command(args, timeout)


def _run_snmpbulkwalk(device: NetworkDevice, oid: str, timeout: int = 20) -> list[str]:
    args = _snmpwalk_args(device, command="snmpbulkwalk", bulk=True) + [oid]
    return _run_snmp_command(args, timeout)


def _run_snmp_command(args: list[str], timeout: int) -> list[str]:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if os.getenv("SNMP_DEBUG") == "1":
        stdout_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        stderr_lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
        logger.info(
            "SNMP debug: cmd=%s rc=%s stdout=%s stderr=%s",
            " ".join(args),
            result.returncode,
            stdout_lines[:10],
            stderr_lines[:10],
        )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "SNMP walk failed.")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _parse_walk(lines: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in lines:
        if " = " not in line:
            continue
        oid_part, value_part = line.split(" = ", 1)
        index = oid_part.split(".")[-1]
        value = value_part.split(": ", 1)[-1].strip().strip('"')
        if value.lower().startswith("no such"):
            continue
        parsed[index] = value
    return parsed


def _parse_status(value: str | None) -> InterfaceStatus:
    if not value:
        return InterfaceStatus.unknown
    lowered = value.lower()
    match = re.search(r"(\d+)", lowered)
    if match:
        code = match.group(1)
        if code == "1":
            return InterfaceStatus.up
        if code == "2":
            return InterfaceStatus.down
    if lowered in {"1", "up", "up(1)"}:
        return InterfaceStatus.up
    if lowered in {"2", "down", "down(2)"}:
        return InterfaceStatus.down
    if "up(" in lowered:
        return InterfaceStatus.up
    if "down(" in lowered:
        return InterfaceStatus.down
    return InterfaceStatus.unknown


def _parse_speed(value: str | None, scale: int = 1) -> int | None:
    if not value:
        return None
    match = re.search(r"(\d+)", value)
    if not match:
        return None
    try:
        parsed = int(match.group(1))
    except ValueError:
        return None
    if scale <= 0:
        return parsed
    return int(parsed / scale)


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(\d+)", value)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _parse_scalar(lines: list[str]) -> str | None:
    if not lines:
        return None
    parsed = _parse_walk(lines)
    if not parsed:
        return None
    return next(iter(parsed.values()))


def collect_device_health(device: NetworkDevice) -> dict[str, float | int | None]:
    uptime_seconds = None
    cpu_percent = None
    memory_percent = None

    uptime_value = _parse_scalar(_run_snmpwalk(device, ".1.3.6.1.2.1.1.3.0"))
    if uptime_value:
        match = re.search(r"\((\d+)\)", uptime_value)
        ticks = int(match.group(1)) if match else _parse_int(uptime_value)
        if ticks is not None:
            uptime_seconds = int(ticks / 100)

    cpu_values = []
    try:
        cpu_table = _parse_walk(_run_snmpbulkwalk(device, ".1.3.6.1.2.1.25.3.3.1.2"))
        for value in cpu_table.values():
            parsed = _parse_int(value)
            if parsed is not None:
                cpu_values.append(parsed)
    except Exception:
        cpu_values = []
    vendor = (device.vendor or "").lower()
    if cpu_values:
        cpu_percent = sum(cpu_values) / len(cpu_values)
    elif "mikrotik" in vendor:
        mikrotik_cpu = _parse_int(_parse_scalar(_run_snmpwalk(device, ".1.3.6.1.4.1.14988.1.1.3.10.0")))
        if mikrotik_cpu is not None:
            cpu_percent = float(mikrotik_cpu)

    try:
        storage_types = _parse_walk(_run_snmpbulkwalk(device, ".1.3.6.1.2.1.25.2.3.1.2"))
        storage_size = _parse_walk(_run_snmpbulkwalk(device, ".1.3.6.1.2.1.25.2.3.1.5"))
        storage_used = _parse_walk(_run_snmpbulkwalk(device, ".1.3.6.1.2.1.25.2.3.1.6"))
        for index, storage_type in storage_types.items():
            if storage_type != ".1.3.6.1.2.1.25.2.1.2":
                continue
            size_val = _parse_int(storage_size.get(index))
            used_val = _parse_int(storage_used.get(index))
            if size_val and used_val is not None:
                memory_percent = (used_val / size_val) * 100.0
                break
    except Exception:
        memory_percent = None

    if memory_percent is None and "mikrotik" in vendor:
        total_mem = _parse_int(_parse_scalar(_run_snmpwalk(device, ".1.3.6.1.4.1.14988.1.1.3.6.0")))
        free_mem = _parse_int(_parse_scalar(_run_snmpwalk(device, ".1.3.6.1.4.1.14988.1.1.3.8.0")))
        if total_mem and free_mem is not None and total_mem > 0:
            used = total_mem - free_mem
            memory_percent = (used / total_mem) * 100.0

    rx_bps = None
    tx_bps = None
    try:
        in_first = _parse_walk(_run_snmpbulkwalk(device, ".1.3.6.1.2.1.31.1.1.1.6"))
        out_first = _parse_walk(_run_snmpbulkwalk(device, ".1.3.6.1.2.1.31.1.1.1.10"))
        start = time.monotonic()
        time.sleep(1)
        in_second = _parse_walk(_run_snmpbulkwalk(device, ".1.3.6.1.2.1.31.1.1.1.6"))
        out_second = _parse_walk(_run_snmpbulkwalk(device, ".1.3.6.1.2.1.31.1.1.1.10"))
        interval = max(time.monotonic() - start, 1)
        rx_total = 0
        tx_total = 0
        for index, value in in_second.items():
            first_val = _parse_int(in_first.get(index))
            second_val = _parse_int(value)
            if first_val is None or second_val is None:
                continue
            delta = max(second_val - first_val, 0)
            rx_total += delta
        for index, value in out_second.items():
            first_val = _parse_int(out_first.get(index))
            second_val = _parse_int(value)
            if first_val is None or second_val is None:
                continue
            delta = max(second_val - first_val, 0)
            tx_total += delta
        if rx_total or tx_total:
            rx_bps = (rx_total * 8) / interval
            tx_bps = (tx_total * 8) / interval
    except Exception:
        rx_bps = None
        tx_bps = None

    return {
        "cpu_percent": cpu_percent,
        "memory_percent": memory_percent,
        "uptime_seconds": uptime_seconds,
        "rx_bps": rx_bps,
        "tx_bps": tx_bps,
    }


def collect_interface_snapshot(device: NetworkDevice) -> list[InterfaceSnapshot]:
    descr = _parse_walk(_run_snmpbulkwalk(device, ".1.3.6.1.2.1.2.2.1.2"))
    alias = _parse_walk(_run_snmpbulkwalk(device, ".1.3.6.1.2.1.31.1.1.1.18"))
    if not alias:
        alias = _parse_walk(_run_snmpbulkwalk(device, ".1.3.6.1.2.1.31.1.1.1.1"))
    status = _parse_walk(_run_snmpbulkwalk(device, ".1.3.6.1.2.1.2.2.1.8"))
    if not status:
        status = _parse_walk(_run_snmpbulkwalk(device, ".1.3.6.1.2.1.2.2.1.7"))
    speed = _parse_walk(_run_snmpbulkwalk(device, ".1.3.6.1.2.1.31.1.1.1.15"))
    speed_bps = {}
    if not speed:
        speed_bps = _parse_walk(_run_snmpbulkwalk(device, ".1.3.6.1.2.1.2.2.1.5"))

    snapshots: list[InterfaceSnapshot] = []
    for index, name in descr.items():
        display_name = name
        description = alias.get(index) or None
        if "=>" in name:
            left, right = name.split("=>", 1)
            display_name = left.strip()
            if not description:
                description = right.strip() or None
        high_speed = _parse_speed(speed.get(index)) if speed else None
        bps_speed = _parse_speed(speed_bps.get(index), scale=1_000_000) if speed_bps else None
        resolved_speed = high_speed if high_speed and high_speed > 0 else bps_speed
        snapshots.append(
            InterfaceSnapshot(
                index=index,
                name=display_name,
                description=description,
                status=_parse_status(status.get(index)),
                speed_mbps=resolved_speed,
            )
        )
    return snapshots


def apply_interface_snapshot(
    db,
    device: NetworkDevice,
    snapshots: list[InterfaceSnapshot],
    create_missing: bool = True,
) -> tuple[int, int]:
    existing = {
        iface.name: iface
        for iface in db.query(DeviceInterface)
        .filter(DeviceInterface.device_id == device.id)
        .all()
    }
    created = 0
    updated = 0
    for snapshot in snapshots:
        iface = existing.get(snapshot.name)
        if iface:
            iface.description = snapshot.description
            iface.status = snapshot.status
            iface.speed_mbps = snapshot.speed_mbps
            updated += 1
        elif create_missing:
            iface = DeviceInterface(
                device_id=device.id,
                name=snapshot.name,
                description=snapshot.description,
                status=snapshot.status,
                speed_mbps=snapshot.speed_mbps,
            )
            db.add(iface)
            created += 1
    db.commit()
    return created, updated
