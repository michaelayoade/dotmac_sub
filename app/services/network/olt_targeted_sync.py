"""Targeted OLT/ONT SNMP reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntAssignment, OntUnit, OnuOnlineStatus
from app.services.network import olt_snmp_sync as bulk_sync
from app.services.network.huawei_snmp import (
    is_huawei_vendor,
    resolve_huawei_snmp_profile,
)
from app.services.network.olt_inventory import get_olt_or_none
from app.services.network.olt_monitoring_devices import resolve_snmp_target_for_olt
from app.services.network.olt_polling import reconcile_snmp_status_with_signal
from app.services.network.ont_assignment_alignment import (
    align_ont_assignment_to_authoritative_fsp,
)
from app.services.network.ont_status import apply_resolved_status_for_model
from app.services.network.snmp_walk import run_simple_v2c_walk


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
    """Sync one known ONT from OLT SNMP into its local inventory row."""
    walk = walk_fn or run_simple_v2c_walk
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found", {}

    ont = db.get(OntUnit, ont_unit_id)
    if ont is None:
        return False, "ONT record not found", {}

    linked = resolve_snmp_target_for_olt(db, olt)
    if not linked:
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
        status_rows = bulk_sync._parse_walk_composite(
            walk(linked, oids["status"], timeout=90, bulk=True)
        )
    except Exception as exc:
        return False, f"SNMP walk failed: {exc!s}", {}

    olt_rx_rows: dict[str, str] = {}
    onu_rx_rows: dict[str, str] = {}
    distance_rows: dict[str, str] = {}
    try:
        olt_rx_rows = bulk_sync._parse_walk_composite(
            walk(linked, oids["olt_rx"], timeout=90, bulk=True)
        )
    except Exception:
        olt_rx_rows = {}
    try:
        onu_rx_rows = bulk_sync._parse_walk_composite(
            walk(linked, oids["onu_rx"], timeout=90, bulk=True)
        )
    except Exception:
        onu_rx_rows = {}
    try:
        distance_rows = bulk_sync._parse_walk_composite(
            walk(linked, oids["distance"], timeout=90, bulk=True)
        )
    except Exception:
        distance_rows = {}

    all_indexes = (
        set(status_rows) | set(olt_rx_rows) | set(onu_rx_rows) | set(distance_rows)
    )
    if not all_indexes:
        return False, "No ONUs discovered from SNMP on this OLT", {}

    packed_fsp_map = (
        bulk_sync._build_packed_fsp_map(db, linked, all_indexes)
        if vendor_key == "huawei"
        else {}
    )
    matched_index: str | None = None

    for idx in sorted(all_indexes):
        parsed = bulk_sync._split_onu_index(idx)
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
                    bulk_sync._decode_huawei_packed_fsp(packed_int)
                    if packed_int is not None
                    else None
                )
                hinted = packed_fsp_map.get(str(packed))
                if decoded and hinted and decoded != hinted:
                    bulk_sync.logger.warning(
                        "Huawei packed FSP hint mismatch on OLT %s for %s: decoded=%s hinted=%s; using hinted",
                        olt.id,
                        packed,
                        decoded,
                        hinted,
                    )
                idx_fsp = str(hinted or decoded or "")
            else:
                idx_fsp = ""

        if idx_fsp == fsp and onu == str(ont_id_on_olt):
            matched_index = idx
            break

    if matched_index is None:
        return False, f"SNMP sync could not find ONT-ID {ont_id_on_olt} on {fsp}.", {}

    olt_rx = bulk_sync._parse_signal_dbm(olt_rx_rows.get(matched_index))
    status, offline_reason, _reconciled = reconcile_snmp_status_with_signal(
        vendor=vendor_key,
        raw_status=status_rows.get(matched_index),
        olt_rx_dbm=olt_rx,
    )
    onu_rx = bulk_sync._parse_signal_dbm(onu_rx_rows.get(matched_index))
    distance = bulk_sync._parse_distance_m(distance_rows.get(matched_index))
    now = datetime.now(UTC)

    try:
        external_id = f"{vendor_key}:{matched_index}"
        conflict = db.scalars(
            select(OntUnit).where(
                OntUnit.olt_device_id == olt.id,
                OntUnit.external_id == external_id,
                OntUnit.id != ont.id,
            )
        ).first()
        if conflict is not None:
            conflict_serial = str(getattr(conflict, "serial_number", "") or "")
            conflict_is_generated = conflict_serial.startswith(
                f"HW-{str(olt.id).split('-')[0].upper()}-"
            ) or conflict_serial.startswith(
                f"{vendor_key.upper()}-{str(olt.id).split('-')[0].upper()}-"
            )
            conflict_has_assignment = db.scalars(
                select(OntAssignment.id).where(
                    OntAssignment.ont_unit_id == conflict.id,
                    OntAssignment.active.is_(True),
                    OntAssignment.subscriber_id.is_not(None),
                )
            ).first()
            if (
                conflict_is_generated
                and not conflict_has_assignment
                and not getattr(conflict, "pppoe_username", None)
            ):
                bulk_sync.logger.warning(
                    "Releasing synthetic ONT %s external_id=%s so real ONT %s can sync",
                    conflict.id,
                    external_id,
                    ont.id,
                )
                conflict.external_id = None
                conflict.is_active = False
            else:
                return (
                    False,
                    f"SNMP sync found {external_id}, but it belongs to another ONT record.",
                    {
                        "matched_index": matched_index,
                        "conflict_ont_id": str(conflict.id),
                    },
                )

        ont.olt_device_id = olt.id
        ont.external_id = external_id
        ont.board = "/".join(fsp.split("/")[:2])
        ont.port = fsp.split("/")[2]
        ont.online_status = status
        ont.olt_rx_signal_dbm = olt_rx
        ont.onu_rx_signal_dbm = onu_rx
        ont.distance_meters = distance
        ont.signal_updated_at = now
        ont.last_seen_at = now if status == OnuOnlineStatus.online else ont.last_seen_at
        ont.offline_reason = (
            None if status == OnuOnlineStatus.online else offline_reason
        )
        ont.last_sync_source = "olt_snmp_targeted"
        ont.last_sync_at = now
        if ont.tr069_acs_server_id is None:
            ont.tr069_acs_server_id = olt.tr069_acs_server_id
        apply_resolved_status_for_model(ont, now=now)
        assignment_alignment = align_ont_assignment_to_authoritative_fsp(
            db,
            ont=ont,
            olt_id=olt.id,
            fsp=fsp,
            assigned_at=now,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        return False, f"Failed to save SNMP sync for ONT: {exc}", {}

    return (
        True,
        f"Synced ONT {ont.serial_number} from OLT SNMP using index {matched_index}.",
        {
            "matched_index": matched_index,
            "assignment_aligned": bool(
                assignment_alignment and assignment_alignment.changed
            ),
        },
    )
