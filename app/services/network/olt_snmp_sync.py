"""SNMP-driven OLT/ONT inventory sync helpers.

This module owns vendor-aware SNMP walks and ONT row reconciliation logic for
bulk OLT sync and targeted post-authorization sync. `web_network_olts.py`
keeps thin wrappers so existing callers and tests continue to use the same
public entrypoints while the heavy sync implementation lives here.
"""

from __future__ import annotations

import logging
import subprocess  # nosec
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    GponChannel,
    OntAssignment,
    OntUnit,
    OnuOfflineReason,
    OnuOnlineStatus,
    PonType,
)
from app.models.network_monitoring import DeviceInterface
from app.services.credential_crypto import decrypt_credential
from app.services.network.huawei_snmp import (
    decode_huawei_packed_fsp,
    is_huawei_vendor,
    resolve_huawei_snmp_profile,
)
from app.services.network.olt_polling import reconcile_snmp_status_with_signal

logger = logging.getLogger(__name__)


def _parse_walk_composite(lines: list[str], *, suffix_parts: int = 4) -> dict[str, str]:
    """Parse SNMP walk output while preserving composite ONU indexes."""
    parsed: dict[str, str] = {}
    for line in lines:
        if " = " not in line:
            continue
        oid_part, value_part = line.split(" = ", 1)
        oid_tokens = [p for p in oid_part.split(".") if p.isdigit()]
        if not oid_tokens:
            continue
        if len(oid_tokens) >= 2 and int(oid_tokens[-2]) > 1_000_000:
            index = f"{oid_tokens[-2]}.{oid_tokens[-1]}"
        else:
            index = (
                ".".join(oid_tokens[-suffix_parts:])
                if len(oid_tokens) >= suffix_parts
                else oid_tokens[-1]
            )
        value = value_part.split(": ", 1)[-1].strip().strip('"')
        if value.lower().startswith("no such"):
            continue
        parsed[index] = value
    return parsed


def _parse_signal_dbm(raw: str | None, scale: float = 0.01) -> float | None:
    if not raw:
        return None
    import re

    match = re.search(r"(-?\d+)", raw)
    if not match:
        return None
    try:
        val = int(match.group(1))
    except ValueError:
        return None
    dbm = val * scale
    if -50.0 <= dbm <= 10.0:
        return dbm
    if -50.0 <= val <= 10.0:
        return float(val)
    return None


def _parse_distance_m(raw: str | None) -> int | None:
    if not raw:
        return None
    import re

    match = re.search(r"(\d+)", raw)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    if value <= 1:
        return None
    return value


def _parse_online_status(
    raw: str | None,
) -> tuple[OnuOnlineStatus, OnuOfflineReason | None]:
    if not raw:
        return OnuOnlineStatus.unknown, None
    import re

    lowered = raw.lower().strip()
    match = re.search(r"(\d+)", lowered)
    code = int(match.group(1)) if match else None
    if code == 1 or "online" in lowered or "up" in lowered:
        return OnuOnlineStatus.online, None
    if code in {2, 3, 4, 5} or "offline" in lowered or "down" in lowered:
        if code == 3:
            return OnuOnlineStatus.offline, OnuOfflineReason.power_fail
        if code == 4:
            return OnuOnlineStatus.offline, OnuOfflineReason.los
        if code == 5:
            return OnuOnlineStatus.offline, OnuOfflineReason.dying_gasp
        return OnuOnlineStatus.offline, OnuOfflineReason.unknown
    return OnuOnlineStatus.unknown, None


def _split_onu_index(raw_index: str) -> tuple[str, ...] | None:
    parts = [p for p in str(raw_index).split(".") if p.isdigit()]
    if len(parts) < 2:
        return None
    if len(parts) >= 4:
        return parts[-4], parts[-3], parts[-2], parts[-1]
    return parts[-2], parts[-1]


def _decode_huawei_packed_fsp(packed_value: int) -> str | None:
    return decode_huawei_packed_fsp(packed_value)


def _external_onu_id(ont: OntUnit) -> str | None:
    raw = str(getattr(ont, "external_id", "") or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return raw
    if ":" in raw:
        raw = raw.rsplit(":", 1)[-1]
    if "." in raw:
        raw = raw.rsplit(".", 1)[-1]
    return raw if raw.isdigit() else None


def _extract_pon_hint(value: str | None) -> str | None:
    import re

    if not value:
        return None
    match = re.search(r"(\d+/\d+/\d+)\s*$", str(value).strip())
    if match:
        return match.group(1)
    return None


def _pon_sort_key(hint: str) -> tuple[int, int, int]:
    parts = hint.split("/")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except Exception:
        return (10**9, 10**9, 10**9)


def _build_packed_fsp_map(
    db: Session, linked: Any, indexes: set[str]
) -> dict[str, str]:
    """Map Huawei packed FSP integers to detected PON hints (0/s/p)."""
    packed_values: list[int] = []
    for idx in indexes:
        parts = [p for p in idx.split(".") if p.isdigit()]
        if len(parts) == 2:
            try:
                packed_values.append(int(parts[0]))
            except ValueError:
                continue
    if not packed_values:
        return {}

    iface_names = list(
        db.scalars(
            select(DeviceInterface.name).where(DeviceInterface.device_id == linked.id)
        ).all()
    )
    hints = sorted(
        {h for name in iface_names if (h := _extract_pon_hint(name))},
        key=_pon_sort_key,
    )
    if not hints:
        return {}

    return {
        str(packed): hint
        for packed, hint in zip(sorted(set(packed_values)), hints, strict=False)
    }


def _run_simple_v2c_walk(
    linked: Any, oid: str, *, timeout: int = 45, bulk: bool = False
) -> list[str]:
    """Run SNMP walk with minimal flags for Huawei compatibility."""
    host = linked.mgmt_ip or linked.hostname
    if not host:
        raise RuntimeError("Missing SNMP host")
    if linked.snmp_port:
        host = f"{host}:{linked.snmp_port}"
    if (linked.snmp_version or "v2c").lower() not in {"v2c", "2c"}:
        raise RuntimeError("Only SNMP v2c is supported for ONT sync")
    community = (
        decrypt_credential(linked.snmp_community) if linked.snmp_community else ""
    )
    if not community:
        raise RuntimeError("SNMP community is not configured")

    cmd = "snmpbulkwalk" if bulk else "snmpwalk"
    args = [cmd, "-v2c", "-c", community, host, oid]
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "SNMP walk failed").strip()
        raise RuntimeError(f"{oid}: {err}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def sync_onts_from_olt_snmp(
    db: Session,
    olt_id: str,
    *,
    walk_fn=None,
) -> tuple[bool, str, dict[str, object]]:
    """Discover ONUs from an OLT by SNMP and upsert OntUnit rows."""
    from app.services import web_network_olts as web_network_olts_service

    walk = walk_fn or web_network_olts_service._run_simple_v2c_walk
    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", {"discovered": 0, "created": 0, "updated": 0}

    linked: Any = web_network_olts_service._find_linked_network_device(
        db,
        mgmt_ip=olt.mgmt_ip,
        hostname=olt.hostname,
        name=olt.name,
    )

    if not linked:
        raw_ro = getattr(olt, "snmp_ro_community", None)
        if raw_ro and raw_ro.strip():
            linked = SimpleNamespace(
                mgmt_ip=olt.mgmt_ip,
                hostname=olt.hostname,
                snmp_enabled=True,
                snmp_community=raw_ro.strip(),
                snmp_version="v2c",
                snmp_port=None,
                vendor=olt.vendor,
            )
        else:
            return (
                False,
                "No linked monitoring device and no SNMP community on OLT",
                {"discovered": 0, "created": 0, "updated": 0},
            )
    if not linked.snmp_enabled:
        return (
            False,
            "SNMP is disabled on the linked monitoring device",
            {"discovered": 0, "created": 0, "updated": 0},
        )

    vendor_text = str(linked.vendor or olt.vendor or "").lower()
    vendor_key = "generic"
    huawei_profile = None
    if is_huawei_vendor(vendor_text):
        huawei_profile = resolve_huawei_snmp_profile(olt.model)
        vendor_key = "huawei"
    elif "zte" in vendor_text:
        vendor_key = "zte"
    elif "nokia" in vendor_text:
        vendor_key = "nokia"

    vendor_oid_profiles: dict[str, dict[str, str]] = {
        "zte": {
            "status": ".1.3.6.1.4.1.3902.1082.500.10.2.2.1.1.10",
            "olt_rx": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.2",
            "onu_rx": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.3",
            "distance": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.7",
        },
        "nokia": {
            "status": ".1.3.6.1.4.1.637.61.1.35.10.1.1.8",
            "olt_rx": ".1.3.6.1.4.1.637.61.1.35.10.14.1.2",
            "onu_rx": ".1.3.6.1.4.1.637.61.1.35.10.14.1.4",
            "distance": ".1.3.6.1.4.1.637.61.1.35.10.1.1.9",
        },
        "generic": {
            "status": ".1.3.6.1.4.1.17409.2.3.6.1.1.8",
            "olt_rx": ".1.3.6.1.4.1.17409.2.3.6.10.1.2",
            "onu_rx": ".1.3.6.1.4.1.17409.2.3.6.10.1.3",
            "distance": ".1.3.6.1.4.1.17409.2.3.6.1.1.9",
        },
    }
    oids = (
        huawei_profile.oids
        if huawei_profile is not None
        else vendor_oid_profiles[vendor_key]
    )

    try:
        walk(linked, ".1.3.6.1.2.1.1.5.0", timeout=20, bulk=False)
        status_rows = _parse_walk_composite(
            walk(linked, oids["status"], timeout=90, bulk=False)
        )
    except Exception as exc:
        return (
            False,
            f"SNMP walk failed: {exc!s}",
            {"discovered": 0, "created": 0, "updated": 0},
        )

    olt_rx_rows: dict[str, str] = {}
    onu_rx_rows: dict[str, str] = {}
    distance_rows: dict[str, str] = {}
    serial_rows: dict[str, str] = {}
    if "serial" in oids:
        try:
            serial_rows = _parse_walk_composite(
                walk(linked, oids["serial"], timeout=90, bulk=False)
            )
        except Exception:
            serial_rows = {}
    try:
        olt_rx_rows = _parse_walk_composite(
            walk(linked, oids["olt_rx"], timeout=90, bulk=False)
        )
    except Exception:
        olt_rx_rows = {}
    try:
        onu_rx_rows = _parse_walk_composite(
            walk(linked, oids["onu_rx"], timeout=90, bulk=False)
        )
    except Exception:
        onu_rx_rows = {}
    try:
        distance_rows = _parse_walk_composite(
            walk(linked, oids["distance"], timeout=90, bulk=False)
        )
    except Exception:
        distance_rows = {}

    all_indexes = (
        set(status_rows) | set(olt_rx_rows) | set(onu_rx_rows) | set(distance_rows)
    )
    if not all_indexes:
        return (
            False,
            "No ONUs discovered from SNMP on this OLT",
            {"discovered": 0, "created": 0, "updated": 0},
        )

    existing_onts = list(
        db.scalars(select(OntUnit).where(OntUnit.olt_device_id == olt.id)).all()
    )
    active_assignment_ont_ids = {
        str(ont_unit_id)
        for ont_unit_id in db.scalars(
            select(OntAssignment.ont_unit_id).where(
                OntAssignment.is_active.is_(True),
                OntAssignment.ont_unit_id.in_([ont.id for ont in existing_onts]),
            )
        ).all()
    }
    by_external_id: dict[str, OntUnit] = {}
    by_fsp_onu: dict[tuple[str, str], OntUnit] = {}
    duplicate_fsp_onu: set[tuple[str, str]] = set()
    by_serial: dict[str, OntUnit] = {}
    by_normalized_serial: dict[str, OntUnit] = {}
    by_vendor_serial: dict[str, OntUnit] = {}
    for ont in existing_onts:
        external_id_val = str(getattr(ont, "external_id", "") or "").strip()
        if external_id_val:
            by_external_id[external_id_val] = web_network_olts_service._prefer_ont_candidate(
                by_external_id.get(external_id_val),
                ont,
                active_assignment_ont_ids=active_assignment_ont_ids,
            )
        serial_val = str(getattr(ont, "serial_number", "") or "").strip()
        if serial_val:
            by_serial[serial_val] = web_network_olts_service._prefer_ont_candidate(
                by_serial.get(serial_val),
                ont,
                active_assignment_ont_ids=active_assignment_ont_ids,
            )
            normalized_serial = web_network_olts_service._normalize_ont_serial(serial_val)
            if normalized_serial:
                by_normalized_serial[normalized_serial] = (
                    web_network_olts_service._prefer_ont_candidate(
                        by_normalized_serial.get(normalized_serial),
                        ont,
                        active_assignment_ont_ids=active_assignment_ont_ids,
                    )
                )
        vendor_serial_val = web_network_olts_service._normalize_ont_serial(
            str(getattr(ont, "vendor_serial_number", "") or "").strip()
        )
        if vendor_serial_val:
            by_vendor_serial[vendor_serial_val] = (
                web_network_olts_service._prefer_ont_candidate(
                    by_vendor_serial.get(vendor_serial_val),
                    ont,
                    active_assignment_ont_ids=active_assignment_ont_ids,
                )
            )
        fsp_hint = None
        board_parts = [p for p in str(getattr(ont, "board", "")).split("/") if p.isdigit()]
        port_parts = [p for p in str(getattr(ont, "port", "")).split("/") if p.isdigit()]
        if len(port_parts) >= 3:
            fsp_hint = f"{port_parts[-3]}/{port_parts[-2]}/{port_parts[-1]}"
        elif len(board_parts) >= 2 and len(port_parts) >= 1:
            fsp_hint = f"{board_parts[-2]}/{board_parts[-1]}/{port_parts[-1]}"
        onu_id = _external_onu_id(ont)
        if fsp_hint and onu_id:
            key = (fsp_hint, onu_id)
            if key in by_fsp_onu:
                duplicate_fsp_onu.add(key)
                by_fsp_onu.pop(key, None)
            elif key not in duplicate_fsp_onu:
                by_fsp_onu[key] = web_network_olts_service._prefer_ont_candidate(
                    by_fsp_onu.get(key),
                    ont,
                    active_assignment_ont_ids=active_assignment_ont_ids,
                )

    created = 0
    updated = 0
    skipped = 0
    now = datetime.now(UTC)
    olt_tag = str(olt.id).split("-")[0].upper()
    packed_fsp_map = (
        _build_packed_fsp_map(db, linked, all_indexes) if vendor_key == "huawei" else {}
    )

    vendor_serial_prefix = {
        "huawei": "HW",
        "zte": "ZT",
        "nokia": "NK",
        "generic": "OLT",
    }.get(vendor_key, "OLT")

    for idx in sorted(all_indexes):
        parsed = _split_onu_index(idx)
        if not parsed:
            continue
        frame = "0"
        slot = "0"
        port = "0"
        onu = "0"
        if len(parsed) >= 4:
            frame, slot, port, onu = parsed
            fsp = f"{frame}/{slot}/{port}"
        else:
            packed, onu = parsed
            if vendor_key == "huawei":
                packed_int = int(packed) if str(packed).isdigit() else None
                decoded = (
                    _decode_huawei_packed_fsp(packed_int)
                    if packed_int is not None
                    else None
                )
                hinted = packed_fsp_map.get(str(packed))
                if decoded and hinted and decoded != hinted:
                    logger.warning(
                        "Huawei packed FSP hint mismatch on OLT %s for %s: decoded=%s hinted=%s; using decoded",
                        olt.id,
                        packed,
                        decoded,
                        hinted,
                    )
                fsp = decoded or hinted or f"0/0/{packed}"
            else:
                fsp = f"0/0/{packed}"
        fsp_parts = fsp.split("/")
        frame = fsp_parts[0] if len(fsp_parts) > 0 else "0"
        slot = fsp_parts[1] if len(fsp_parts) > 1 else "0"
        port = fsp_parts[2] if len(fsp_parts) > 2 else "0"
        board = f"{frame}/{slot}"
        external_id = f"{vendor_key}:{idx}"
        synthetic_serial = f"{vendor_serial_prefix}-{olt_tag}-{frame}{slot}{port}{onu}"
        vendor_serial = (
            web_network_olts_service._normalize_ont_serial(
                str(serial_rows.get(idx) or "").strip()
            )
            or None
        )

        olt_rx = _parse_signal_dbm(olt_rx_rows.get(idx))
        status, offline_reason, _reconciled = reconcile_snmp_status_with_signal(
            vendor=vendor_key,
            raw_status=status_rows.get(idx),
            olt_rx_dbm=olt_rx,
        )
        onu_rx = _parse_signal_dbm(onu_rx_rows.get(idx))
        distance = _parse_distance_m(distance_rows.get(idx))

        vs_key = vendor_serial or ""
        matched_ont: OntUnit | None = None
        if vendor_key == "huawei":
            matched_ont = (
                by_fsp_onu.get((fsp, onu))
                or by_external_id.get(external_id)
                or (vs_key and by_vendor_serial.get(vs_key))
                or by_normalized_serial.get(vs_key)
                or by_serial.get(synthetic_serial)
            ) or None
        else:
            matched_ont = (
                by_external_id.get(external_id)
                or by_fsp_onu.get((fsp, onu))
                or (vs_key and by_vendor_serial.get(vs_key))
                or by_normalized_serial.get(vs_key)
                or by_serial.get(synthetic_serial)
            ) or None
        if matched_ont is None:
            ont = OntUnit(
                serial_number=synthetic_serial,
                vendor_serial_number=vendor_serial,
                model=olt.model,
                vendor=olt.vendor or vendor_key.title(),
                is_active=True,
                olt_device_id=olt.id,
                pon_type=PonType.gpon,
                gpon_channel=GponChannel.gpon,
                board=board,
                port=port,
                external_id=external_id,
            )
            db.add(ont)
            created += 1
        else:
            ont = matched_ont
            if not ont.is_active:
                logger.info(
                    "Inactive ONT %s (external_id=%s) rediscovered during sync on OLT %s",
                    ont.id,
                    ont.external_id,
                    olt.id,
                )
            ont.is_active = True
            ont.olt_device_id = olt.id
            ont.board = board
            ont.port = port
            ont.external_id = external_id
            updated += 1

        if (
            vendor_serial
            and not web_network_olts_service._looks_synthetic_ont_serial(vendor_serial)
            and web_network_olts_service._is_plausible_vendor_serial(vendor_serial)
        ):
            ont.vendor_serial_number = vendor_serial
            if (
                not getattr(ont, "serial_number", None)
                or web_network_olts_service._looks_synthetic_ont_serial(ont.serial_number)
            ):
                ont.serial_number = vendor_serial
        elif not getattr(ont, "vendor_serial_number", None):
            ont.vendor_serial_number = vendor_serial
        if not getattr(ont, "serial_number", None):
            ont.serial_number = synthetic_serial
        ont.online_status = status
        ont.offline_reason = None if status == OnuOnlineStatus.online else offline_reason
        ont.olt_rx_signal_dbm = olt_rx
        ont.onu_rx_signal_dbm = onu_rx
        ont.distance_meters = distance
        ont.signal_updated_at = now
        ont.last_seen_at = now if status == OnuOnlineStatus.online else ont.last_seen_at
        if ont.tr069_acs_server_id is None:
            ont.tr069_acs_server_id = olt.tr069_acs_server_id

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        return (
            False,
            f"Failed to save SNMP sync results: {exc}",
            {"discovered": len(all_indexes), "created": created, "updated": updated},
        )

    return (
        True,
        f"Discovered {len(all_indexes)} ONTs from SNMP.",
        {
            "discovered": len(all_indexes),
            "created": created,
            "updated": updated,
            "skipped": skipped,
        },
    )


def sync_authorized_ont_from_olt_snmp(
    db: Session,
    *,
    olt_id: str,
    ont_unit_id: str,
    fsp: str,
    ont_id_on_olt: int,
    serial_number: str,
    walk_fn=None,
) -> tuple[bool, str, dict[str, object]]:
    """Sync only the just-authorized ONT from OLT SNMP into the local ONT row."""
    from app.services import web_network_olts as web_network_olts_service

    walk = walk_fn or web_network_olts_service._run_simple_v2c_walk
    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", {}

    ont = db.get(OntUnit, ont_unit_id)
    if ont is None:
        return False, "ONT record not found", {}

    linked: Any = web_network_olts_service._find_linked_network_device(
        db,
        mgmt_ip=olt.mgmt_ip,
        hostname=olt.hostname,
        name=olt.name,
    )
    if not linked:
        raw_ro = getattr(olt, "snmp_ro_community", None)
        if raw_ro and raw_ro.strip():
            linked = SimpleNamespace(
                mgmt_ip=olt.mgmt_ip,
                hostname=olt.hostname,
                snmp_enabled=True,
                snmp_community=raw_ro.strip(),
                snmp_version="v2c",
                snmp_port=None,
                vendor=olt.vendor,
            )
        else:
            return False, "No linked monitoring device and no SNMP community on OLT", {}
    if not linked.snmp_enabled:
        return False, "SNMP is disabled on the linked monitoring device", {}

    vendor_text = str(linked.vendor or olt.vendor or "").lower()
    vendor_key = "generic"
    huawei_profile = None
    if is_huawei_vendor(vendor_text):
        huawei_profile = resolve_huawei_snmp_profile(olt.model)
        vendor_key = "huawei"
    elif "zte" in vendor_text:
        vendor_key = "zte"
    elif "nokia" in vendor_text:
        vendor_key = "nokia"

    vendor_oid_profiles: dict[str, dict[str, str]] = {
        "zte": {
            "status": ".1.3.6.1.4.1.3902.1082.500.10.2.2.1.1.10",
            "olt_rx": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.2",
            "onu_rx": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.3",
            "distance": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.7",
        },
        "nokia": {
            "status": ".1.3.6.1.4.1.637.61.1.35.10.1.1.8",
            "olt_rx": ".1.3.6.1.4.1.637.61.1.35.10.14.1.2",
            "onu_rx": ".1.3.6.1.4.1.637.61.1.35.10.14.1.4",
            "distance": ".1.3.6.1.4.1.637.61.1.35.10.1.1.9",
        },
        "generic": {
            "status": ".1.3.6.1.4.1.17409.2.3.6.1.1.8",
            "olt_rx": ".1.3.6.1.4.1.17409.2.3.6.10.1.2",
            "onu_rx": ".1.3.6.1.4.1.17409.2.3.6.10.1.3",
            "distance": ".1.3.6.1.4.1.17409.2.3.6.1.1.9",
        },
    }
    oids = (
        huawei_profile.oids
        if huawei_profile is not None
        else vendor_oid_profiles[vendor_key]
    )

    try:
        walk(linked, ".1.3.6.1.2.1.1.5.0", timeout=20, bulk=False)
        status_rows = _parse_walk_composite(
            walk(linked, oids["status"], timeout=90, bulk=False)
        )
    except Exception as exc:
        return False, f"SNMP walk failed: {exc!s}", {}

    olt_rx_rows: dict[str, str] = {}
    onu_rx_rows: dict[str, str] = {}
    distance_rows: dict[str, str] = {}
    try:
        olt_rx_rows = _parse_walk_composite(
            walk(linked, oids["olt_rx"], timeout=90, bulk=False)
        )
    except Exception:
        olt_rx_rows = {}
    try:
        onu_rx_rows = _parse_walk_composite(
            walk(linked, oids["onu_rx"], timeout=90, bulk=False)
        )
    except Exception:
        onu_rx_rows = {}
    try:
        distance_rows = _parse_walk_composite(
            walk(linked, oids["distance"], timeout=90, bulk=False)
        )
    except Exception:
        distance_rows = {}

    all_indexes = (
        set(status_rows) | set(olt_rx_rows) | set(onu_rx_rows) | set(distance_rows)
    )
    if not all_indexes:
        return False, "No ONUs discovered from SNMP on this OLT", {}

    packed_fsp_map = (
        _build_packed_fsp_map(db, linked, all_indexes) if vendor_key == "huawei" else {}
    )
    matched_index: str | None = None

    for idx in sorted(all_indexes):
        parsed = _split_onu_index(idx)
        if not parsed:
            continue
        idx_fsp = ""
        onu = ""
        if len(parsed) >= 4:
            idx_fsp = f"{parsed[0]}/{parsed[1]}/{parsed[2]}"
            onu = parsed[3]
        else:
            packed, onu = parsed
            if vendor_key == "huawei":
                packed_int = int(packed) if str(packed).isdigit() else None
                decoded = (
                    _decode_huawei_packed_fsp(packed_int) if packed_int is not None else None
                )
                hinted = packed_fsp_map.get(str(packed))
                if decoded and hinted and decoded != hinted:
                    logger.warning(
                        "Huawei packed FSP hint mismatch on OLT %s for %s: decoded=%s hinted=%s; using decoded",
                        olt.id,
                        packed,
                        decoded,
                        hinted,
                    )
                idx_fsp = decoded or hinted or f"0/0/{packed}"
            else:
                idx_fsp = f"0/0/{packed}"

        if idx_fsp == fsp and onu == str(ont_id_on_olt):
            matched_index = idx
            break

    if matched_index is None:
        return False, f"SNMP sync could not find ONT-ID {ont_id_on_olt} on {fsp}.", {}

    olt_rx = _parse_signal_dbm(olt_rx_rows.get(matched_index))
    status, offline_reason, _reconciled = reconcile_snmp_status_with_signal(
        vendor=vendor_key,
        raw_status=status_rows.get(matched_index),
        olt_rx_dbm=olt_rx,
    )
    onu_rx = _parse_signal_dbm(onu_rx_rows.get(matched_index))
    distance = _parse_distance_m(distance_rows.get(matched_index))
    now = datetime.now(UTC)

    try:
        ont.olt_device_id = olt.id
        ont.external_id = f"{vendor_key}:{matched_index}"
        ont.board = "/".join(fsp.split("/")[:2])
        ont.port = fsp.split("/")[2]
        ont.online_status = status
        ont.olt_rx_signal_dbm = olt_rx
        ont.onu_rx_signal_dbm = onu_rx
        ont.distance_meters = distance
        ont.signal_updated_at = now
        ont.last_seen_at = now if status == OnuOnlineStatus.online else ont.last_seen_at
        ont.offline_reason = None if status == OnuOnlineStatus.online else offline_reason
        if ont.tr069_acs_server_id is None:
            ont.tr069_acs_server_id = olt.tr069_acs_server_id
        db.commit()
    except Exception as exc:
        db.rollback()
        return False, f"Failed to save SNMP sync for ONT: {exc}", {}

    return (
        True,
        f"Synced ONT {ont.serial_number} from OLT SNMP using index {matched_index}.",
        {"matched_index": matched_index},
    )
