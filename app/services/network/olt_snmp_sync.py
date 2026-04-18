"""SNMP-driven OLT/ONU telemetry sync helpers.

This module owns vendor-aware SNMP walks and ONT row reconciliation logic for
bulk OLT telemetry sync and targeted post-authorization sync. `web_network_olts.py`
keeps thin wrappers so existing callers and tests continue to use the same
public entrypoints while the heavy sync implementation lives here.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import (
    GponChannel,
    OntAssignment,
    OntUnit,
    OnuOfflineReason,
    OnuOnlineStatus,
    PonType,
)
from app.models.network_monitoring import DeviceInterface
from app.services import tr069 as tr069_service
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network.huawei_snmp import (
    decode_huawei_packed_fsp,
    is_huawei_vendor,
    resolve_huawei_snmp_profile,
)
from app.services.network.olt_inventory import get_olt_or_none
from app.services.network.olt_monitoring_devices import resolve_snmp_target_for_olt
from app.services.network.olt_polling import reconcile_snmp_status_with_signal
from app.services.network.olt_tr069_admin import queue_acs_propagation
from app.services.network.olt_web_audit import (
    actor_name_from_request,
    log_olt_audit_event,
)
from app.services.network.olt_web_topology import ensure_canonical_pon_port
from app.services.network.ont_assignment_alignment import (
    align_ont_assignment_to_authoritative_fsp,
)
from app.services.network.ont_serials import (
    is_plausible_vendor_serial,
    looks_synthetic_ont_serial,
    normalize_ont_serial,
    prefer_ont_candidate,
)
from app.services.network.ont_status import (
    apply_status_snapshot,
    resolve_ont_status_for_model,
)
from app.services.network.snmp_walk import run_simple_v2c_walk

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
    linked_id = getattr(linked, "id", None)
    if linked_id is None:
        return {}

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
            select(DeviceInterface.name).where(DeviceInterface.device_id == linked_id)
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
    """Compatibility wrapper for the shared SNMP walk helper."""
    return run_simple_v2c_walk(linked, oid, timeout=timeout, bulk=bulk)


def sync_onts_from_olt_snmp(
    db: Session,
    olt_id: str,
    *,
    walk_fn=None,
) -> tuple[bool, str, dict[str, object]]:
    """Discover ONUs from an OLT by SNMP with a per-OLT advisory lock."""
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        if walk_fn is None:
            return _sync_onts_from_olt_snmp_impl(db, olt_id)
        return _sync_onts_from_olt_snmp_impl(db, olt_id, walk_fn=walk_fn)
    lock_key = olt_sync_lock_key(olt_id)
    lock_acquired = bool(
        db.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_key}
        ).scalar()
    )
    if not lock_acquired:
        return (
            False,
            "Another sync is already running for this OLT",
            {"discovered": 0, "created": 0, "updated": 0},
        )
    if walk_fn is None:
        return _sync_onts_from_olt_snmp_impl(db, olt_id)
    return _sync_onts_from_olt_snmp_impl(db, olt_id, walk_fn=walk_fn)


def _sync_onts_from_olt_snmp_impl(
    db: Session,
    olt_id: str,
    *,
    walk_fn=None,
) -> tuple[bool, str, dict[str, object]]:
    """Internal implementation of ONT SNMP sync."""
    walk = walk_fn or _run_simple_v2c_walk
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", {"discovered": 0, "created": 0, "updated": 0}

    linked: Any = resolve_snmp_target_for_olt(db, olt)
    if not linked:
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
    existing_ont_ids = [ont.id for ont in existing_onts]
    active_assignment_ont_ids = (
        {
            str(ont_unit_id)
            for ont_unit_id in db.scalars(
                select(OntAssignment.ont_unit_id).where(
                    OntAssignment.active.is_(True),
                    OntAssignment.ont_unit_id.in_(existing_ont_ids),
                )
            ).all()
        }
        if existing_ont_ids
        else set()
    )
    by_external_id: dict[str, OntUnit] = {}
    by_fsp_onu: dict[tuple[str, str], OntUnit] = {}
    duplicate_fsp_onu: set[tuple[str, str]] = set()
    by_serial: dict[str, OntUnit] = {}
    by_normalized_serial: dict[str, OntUnit] = {}
    by_vendor_serial: dict[str, OntUnit] = {}
    for ont in existing_onts:
        external_id_val = str(getattr(ont, "external_id", "") or "").strip()
        if external_id_val:
            by_external_id[external_id_val] = prefer_ont_candidate(
                by_external_id.get(external_id_val),
                ont,
                active_assignment_ont_ids=active_assignment_ont_ids,
            )
        serial_val = str(getattr(ont, "serial_number", "") or "").strip()
        if serial_val:
            by_serial[serial_val] = prefer_ont_candidate(
                by_serial.get(serial_val),
                ont,
                active_assignment_ont_ids=active_assignment_ont_ids,
            )
            normalized_serial = normalize_ont_serial(serial_val)
            if normalized_serial:
                by_normalized_serial[normalized_serial] = prefer_ont_candidate(
                    by_normalized_serial.get(normalized_serial),
                    ont,
                    active_assignment_ont_ids=active_assignment_ont_ids,
                )
        vendor_serial_val = normalize_ont_serial(
            str(getattr(ont, "vendor_serial_number", "") or "").strip()
        )
        if vendor_serial_val:
            by_vendor_serial[vendor_serial_val] = prefer_ont_candidate(
                by_vendor_serial.get(vendor_serial_val),
                ont,
                active_assignment_ont_ids=active_assignment_ont_ids,
            )
        fsp_hint = None
        board_parts = [
            p for p in str(getattr(ont, "board", "")).split("/") if p.isdigit()
        ]
        port_parts = [
            p for p in str(getattr(ont, "port", "")).split("/") if p.isdigit()
        ]
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
                by_fsp_onu[key] = prefer_ont_candidate(
                    by_fsp_onu.get(key),
                    ont,
                    active_assignment_ont_ids=active_assignment_ont_ids,
                )

    created = 0
    updated = 0
    skipped = 0
    unresolved_topology = 0
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
        frame: str | None = None
        slot: str | None = None
        port: str | None = None
        onu = "0"
        fsp: str | None = None
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
                        "Huawei packed FSP hint mismatch on OLT %s for %s: decoded=%s hinted=%s; using hinted",
                        olt.id,
                        packed,
                        decoded,
                        hinted,
                    )
                fsp = hinted or decoded
        if fsp:
            fsp_parts = fsp.split("/")
            frame = fsp_parts[0] if len(fsp_parts) > 0 else None
            slot = fsp_parts[1] if len(fsp_parts) > 1 else None
            port = fsp_parts[2] if len(fsp_parts) > 2 else None
        else:
            unresolved_topology += 1
        board = f"{frame}/{slot}" if frame is not None and slot is not None else None
        external_id = f"{vendor_key}:{idx}"
        serial_frame = frame if frame is not None else "U"
        serial_slot = slot if slot is not None else "U"
        serial_port = port if port is not None else "U"
        synthetic_serial = f"{vendor_serial_prefix}-{olt_tag}-{serial_frame}{serial_slot}{serial_port}{onu}"
        vendor_serial = (
            normalize_ont_serial(str(serial_rows.get(idx) or "").strip()) or None
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
        fsp_onu_key = (fsp, onu) if fsp is not None else None
        matched_ont: OntUnit | None = None
        if vendor_key == "huawei":
            matched_ont = (
                (by_fsp_onu.get(fsp_onu_key) if fsp_onu_key else None)
                or by_external_id.get(external_id)
                or (vs_key and by_vendor_serial.get(vs_key))
                or by_normalized_serial.get(vs_key)
                or by_serial.get(synthetic_serial)
            ) or None
        else:
            matched_ont = (
                by_external_id.get(external_id)
                or (by_fsp_onu.get(fsp_onu_key) if fsp_onu_key else None)
                or (vs_key and by_vendor_serial.get(vs_key))
                or by_normalized_serial.get(vs_key)
                or by_serial.get(synthetic_serial)
            ) or None
        if matched_ont is None:
            skipped += 1
            logger.info(
                "Skipping SNMP-only ONU observation on OLT %s index=%s fsp=%s onu=%s vendor_serial=%s; "
                "ONT inventory is created by autofind authorization, manual add, or explicit import.",
                olt.id,
                idx,
                fsp,
                onu,
                vendor_serial,
            )
            continue

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
        ont.vendor = ont.vendor or (olt.vendor or vendor_key.title())
        ont.model = ont.model or olt.model
        ont.board = board
        ont.port = port
        ont.external_id = external_id
        ont.pon_type = PonType.gpon
        ont.gpon_channel = GponChannel.gpon
        tr069_service.sync_ont_acs_server(db, ont, olt.tr069_acs_server_id)
        updated += 1

        if (
            vendor_serial
            and not looks_synthetic_ont_serial(vendor_serial)
            and is_plausible_vendor_serial(vendor_serial)
        ):
            ont.vendor_serial_number = vendor_serial
            if not getattr(ont, "serial_number", None) or looks_synthetic_ont_serial(
                ont.serial_number
            ):
                ont.serial_number = vendor_serial
        elif not getattr(ont, "vendor_serial_number", None):
            ont.vendor_serial_number = vendor_serial
        ont.online_status = status
        if status == OnuOnlineStatus.online:
            ont.last_seen_at = now
            ont.offline_reason = None
        elif status == OnuOnlineStatus.offline:
            ont.offline_reason = offline_reason
        else:
            ont.offline_reason = None
        ont.olt_rx_signal_dbm = olt_rx
        ont.onu_rx_signal_dbm = onu_rx
        ont.distance_meters = distance
        ont.signal_updated_at = now
        if fsp and board and port:
            ensure_canonical_pon_port(
                db,
                olt_id=olt.id,
                fsp=fsp,
                board=board,
                port=port,
            )
        apply_status_snapshot(
            ont,
            resolve_ont_status_for_model(
                ont,
                now=now,
            ),
        )

    try:
        db.flush()
    except Exception as exc:
        db.rollback()
        return (
            False,
            f"Failed to save discovered ONTs: {exc!s}",
            {"discovered": len(all_indexes), "created": created, "updated": updated},
        )

    assignment_created = 0
    assignment_updated = 0
    assignment_reactivated = 0
    assignment_errors = 0
    try:
        active_onts = list(
            db.scalars(
                select(OntUnit).where(
                    OntUnit.olt_device_id == olt.id,
                    OntUnit.is_active.is_(True),
                )
            ).all()
        )
        for ont_item in active_onts:
            ont_board = getattr(ont_item, "board", "") or ""
            ont_port = getattr(ont_item, "port", "") or ""
            pon_name = f"{ont_board}/{ont_port}" if ont_board and ont_port else None
            if not pon_name:
                continue
            alignment = align_ont_assignment_to_authoritative_fsp(
                db,
                ont=ont_item,
                olt_id=olt.id,
                fsp=pon_name,
                assigned_at=now,
            )
            if alignment is None:
                continue
            if alignment.created:
                assignment_created += 1
            elif alignment.reactivated:
                assignment_reactivated += 1
            elif alignment.updated:
                assignment_updated += 1
        if assignment_created or assignment_reactivated or assignment_updated:
            db.flush()
    except Exception as exc:
        logger.warning("Failed to align ONT assignments to OLT scan: %s", exc)
        db.rollback()
        assignment_errors += 1
        return (
            False,
            f"Failed to align ONT assignments to OLT scan: {exc!s}",
            {
                "discovered": len(all_indexes),
                "created": created,
                "updated": updated,
                "assignments_created": 0,
                "assignments_updated": 0,
                "assignments_reactivated": 0,
                "assignment_errors": assignment_errors,
                "tr069_runtime_synced": 0,
                "tr069_runtime_errors": 0,
            },
        )

    if created > 0:
        try:
            emit_event(
                db,
                EventType.ont_discovered,
                {
                    "olt_id": str(olt.id),
                    "olt_name": olt.name,
                    "created": created,
                    "updated": updated,
                    "total_discovered": len(all_indexes),
                },
                actor="system",
            )
        except Exception as exc:
            logger.warning("Failed to emit ont_discovered event: %s", exc)

    tr069_runtime_synced = 0
    tr069_runtime_errors = 0
    if olt.tr069_acs_server_id:
        try:
            from app.services.network.ont_tr069 import OntTR069

            onts_for_olt = list(
                db.scalars(
                    select(OntUnit)
                    .where(OntUnit.olt_device_id == olt.id)
                    .where(OntUnit.is_active.is_(True))
                ).all()
            )
            for ont in onts_for_olt:
                try:
                    summary = OntTR069.get_device_summary(
                        db,
                        str(ont.id),
                        persist_observed_runtime=False,
                    )
                    if summary.available:
                        OntTR069._persist_observed_runtime(
                            db,
                            ont,
                            summary,
                            commit=False,
                        )
                        tr069_runtime_synced += 1
                except Exception:
                    tr069_runtime_errors += 1
        except Exception:
            tr069_runtime_errors += 1

    propagation_stats: dict[str, int] = {}
    if olt.tr069_acs_server_id:
        try:
            propagation_stats = queue_acs_propagation(db, olt)
        except Exception as exc:
            logger.error("ACS propagation after ONU telemetry sync failed: %s", exc)
            propagation_stats = {
                "attempted": 0,
                "propagated": 0,
                "unresolved": 0,
                "errors": 1,
            }

    result_stats: dict[str, object] = {
        "discovered": len(all_indexes),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "unresolved_topology": unresolved_topology,
        "assignments_created": assignment_created,
        "assignments_updated": assignment_updated,
        "assignments_reactivated": assignment_reactivated,
        "assignment_errors": assignment_errors,
        "tr069_runtime_synced": tr069_runtime_synced,
        "tr069_runtime_errors": tr069_runtime_errors,
    }
    if propagation_stats:
        result_stats["acs_propagation"] = propagation_stats

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        return False, f"Failed to finalize ONU telemetry sync: {exc!s}", result_stats

    return (
        True,
        f"{vendor_key.title()} ONU telemetry sync complete: observed {len(all_indexes)}, "
        f"updated {updated}, skipped {skipped} unlinked.",
        result_stats,
    )


def sync_onts_from_olt_snmp_tracked(
    db: Session,
    olt_id: str,
    *,
    initiated_by: str | None = None,
    request: Request | None = None,
    walk_fn=None,
    sync_fn=None,
) -> tuple[bool, str, dict[str, object]]:
    """Tracked wrapper around SNMP-driven ONU telemetry sync."""
    from app.models.network_operation import (
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.network_operations import network_operations

    initiated_by = initiated_by or actor_name_from_request(request)
    try:
        op = network_operations.start(
            db,
            NetworkOperationType.olt_ont_sync,
            NetworkOperationTargetType.olt,
            olt_id,
            correlation_key=f"olt_sync:{olt_id}",
            initiated_by=initiated_by,
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            return False, "A sync is already in progress for this OLT.", {}
        raise
    network_operations.mark_running(db, str(op.id))
    db.flush()

    try:
        sync = sync_fn or sync_onts_from_olt_snmp
        if sync_fn is None:
            success, message, stats = sync(db, olt_id, walk_fn=walk_fn)
        else:
            success, message, stats = sync(db, olt_id)
        try:
            if success:
                network_operations.mark_succeeded(
                    db, str(op.id), output_payload=dict(stats)
                )
            else:
                network_operations.mark_failed(
                    db, str(op.id), message, output_payload=dict(stats)
                )
        except Exception as track_err:
            logger.error(
                "Failed to record operation outcome for %s: %s", op.id, track_err
            )
        log_olt_audit_event(
            db,
            request=request,
            action="sync_onts",
            entity_id=olt_id,
            metadata={
                "result": "success" if success else "error",
                "message": message,
                "stats": stats,
            },
            status_code=200 if success else 500,
            is_success=success,
        )
        return success, message, stats
    except Exception as exc:
        try:
            network_operations.mark_failed(db, str(op.id), str(exc))
        except Exception as track_err:
            logger.error(
                "Failed to record operation failure for %s: %s (original: %s)",
                op.id,
                track_err,
                exc,
            )
            db.rollback()
        raise


def olt_sync_lock_key(olt_id: str) -> int:
    """Return a deterministic positive advisory-lock key for an OLT id."""
    import hashlib

    digest = hashlib.sha256(str(olt_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False) & 0x7FFFFFFFFFFFFFFF


_olt_sync_lock_key = olt_sync_lock_key


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
    """Compatibility wrapper for the targeted SNMP sync service."""
    from app.services.network.olt_targeted_sync import (
        sync_authorized_ont_from_olt_snmp as _sync_authorized_ont_from_olt_snmp,
    )

    return _sync_authorized_ont_from_olt_snmp(
        db,
        olt_id=olt_id,
        ont_unit_id=ont_unit_id,
        fsp=fsp,
        ont_id_on_olt=ont_id_on_olt,
        serial_number=serial_number,
        walk_fn=walk_fn,
    )
