"""Migrate SmartOLT ONTs to GenieACS management with full customer linkage.

Comprehensive migration that:

1. Syncs GenieACS device list into tr069_cpe_devices table
2. Links CPE records to existing OntUnit records by serial number
3. Links ONT assignments to subscribers via PPPoE username → access_credentials
4. Pushes PPPoE credentials to online devices via TR-069
5. Pushes WiFi SSID configuration via TR-069
6. Tags devices in GenieACS as "migrated"
7. Updates OntUnit.provisioning_status to "provisioned"

The bootstrap provision (deployed separately) handles connection request
credentials for all devices automatically on every inform session.

Usage:
    poetry run python scripts/migration/migrate_smartolt_to_genieacs.py --dry-run
    poetry run python scripts/migration/migrate_smartolt_to_genieacs.py --execute
    poetry run python scripts/migration/migrate_smartolt_to_genieacs.py --execute --skip-tr069-push
    poetry run python scripts/migration/migrate_smartolt_to_genieacs.py --execute --only-step 2
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from scripts.migration.db_connections import dotmac_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("migrate_smartolt_genieacs")


@dataclass
class MigrationStats:
    """Track migration progress across all steps."""

    # Step 0 — Serial fix + CSV import
    serials_fixed: int = 0
    onts_created: int = 0
    pppoe_filled: int = 0

    # Step 1 — GenieACS sync
    genieacs_devices: int = 0
    cpe_created: int = 0
    cpe_updated: int = 0
    cpe_auto_linked: int = 0

    # Step 2 — Subscriber linkage
    assignments_linked: int = 0
    assignments_already_linked: int = 0
    assignments_no_match: int = 0

    # Step 3 — TR-069 push (PPPoE + WiFi)
    pppoe_pushed: int = 0
    pppoe_failed: int = 0
    pppoe_skipped: int = 0
    wifi_pushed: int = 0
    wifi_failed: int = 0
    wifi_skipped: int = 0
    data_model_cached: int = 0
    tagged: int = 0

    # Step 4 — Provisioning status
    provisioned: int = 0
    already_provisioned: int = 0

    errors: list[str] = field(default_factory=list)


def _normalize_serial(value: str | None) -> str:
    """Strip non-alphanumeric chars and uppercase for matching."""
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()


# ── SmartOLT → DotMac OLT name mapping ───────────────────────────────────────

_OLT_NAME_MAP: dict[str, str] = {
    "Karasana OLT 1": "Karsana Huawei OLT",
    "Jabi OLT-1": "Jabi Huawei OLT",
    "Garki Huawei OLT": "Garki Huawei OLT",
    "SPDC OLT": "SPDC Huawei OLT",
    "Gudu OLT": "Gudu Huawei OLT",
    "Gwarimpa Huawei OLT 2": "Gwarimpa Huawei OLT",
    "BOI Asokoro OLT 1": "BOI Huawei OLT",
    "Allen": "Allen Huawei OLT",
}


# ── Step 0: Fix synthetic serials + import missing ONTs from CSV ─────────────


def step0_fix_serials_and_import(
    db,
    csv_path: str | None,
    *,
    dry_run: bool,
    stats: MigrationStats,
) -> None:
    """Fix HW- synthetic serials and import missing ONTs from SmartOLT CSV.

    SNMP discovery creates ONTs with synthetic serials like HW-363D7BE1-0200
    that don't match GenieACS device IDs. This step:
    1. Matches HW- ONTs to SmartOLT CSV entries by OLT + board/port/ONU position
    2. Updates serial to real Huawei format (HWTC.../HWTT...)
    3. Fills PPPoE credentials from CSV
    4. Creates new ONTs for CSV entries not yet in our DB
    """
    logger.info("=" * 60)
    logger.info("Step 0: Fix synthetic serials + import from CSV")
    logger.info("=" * 60)

    if not csv_path or not Path(csv_path).exists():
        logger.warning("  No SmartOLT CSV provided, skipping step 0")
        return

    # Load OLT name → ID mapping
    olt_rows = db.execute(
        text("SELECT id, name FROM olt_devices WHERE is_active = true")
    ).fetchall()
    olt_id_by_name: dict[str, str] = {name: str(oid) for oid, name in olt_rows}

    # Build CSV lookup: (olt_name, board, port, onu_id) → row data
    csv_by_position: dict[tuple[str, str, str, str], dict] = {}
    csv_by_serial: dict[str, dict] = {}
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            sn = (row.get("SN") or "").strip()
            olt = (row.get("OLT") or "").strip()
            board = (row.get("Board") or "").strip()
            port = (row.get("Port") or "").strip()
            onu_id = (row.get("Allocated ONU") or "").strip()
            if sn:
                csv_by_serial[sn.upper()] = row
            if olt and board and port and onu_id:
                dotmac_olt = _OLT_NAME_MAP.get(olt, olt)
                csv_by_position[(dotmac_olt, board, port, onu_id)] = row
    logger.info(
        "  Loaded %d CSV entries (%d with position)",
        len(csv_by_serial),
        len(csv_by_position),
    )

    # Phase 1: Fix HW- synthetic serials by matching OLT position
    hw_onts = db.execute(
        text(
            "SELECT id, serial_number, olt_device_id, board, port "
            "FROM ont_units WHERE serial_number LIKE 'HW-%' AND is_active = true"
        )
    ).fetchall()
    logger.info("  Found %d HW- synthetic serials to fix", len(hw_onts))

    for ont_id, hw_serial, olt_device_id, board_slot, port_val in hw_onts:
        if not olt_device_id or not board_slot or not port_val:
            continue

        # Parse board/port — our DB stores board as "0/2", port as "0"
        # SmartOLT CSV uses Board=1, Port=0, ONU=0
        # OLT board is typically slot, our "board" field is "frame/slot"
        olt_name = None
        for name, oid in olt_id_by_name.items():
            if oid == str(olt_device_id):
                olt_name = name
                break
        if not olt_name:
            continue

        # Extract ONU ID from HW- serial: HW-{hex}-{SSPP} where SS=slot+port, PP=onu
        # Example: HW-363D7BE1-0200 → slot/port encoded as 02, onu as 00
        serial_suffix = hw_serial.split("-")[-1] if "-" in hw_serial else ""
        if len(serial_suffix) != 4:
            continue

        # Parse: first 2 digits = port index, last 2 = onu id
        try:
            port_idx = int(serial_suffix[:2])
            onu_idx = int(serial_suffix[2:])
        except ValueError:
            continue

        # Match CSV using our board's slot + port index
        # board="0/2" → slot=2, then port from port_idx
        slot = board_slot.split("/")[-1] if "/" in board_slot else board_slot
        csv_key = (olt_name, slot, str(port_idx), str(onu_idx))
        csv_row = csv_by_position.get(csv_key)
        if not csv_row:
            continue

        real_serial = (csv_row.get("SN") or "").strip()
        if not real_serial:
            continue

        # Check real serial doesn't already exist
        existing = db.execute(
            text(
                "SELECT id FROM ont_units WHERE UPPER(serial_number) = UPPER(:sn) AND is_active = true"
            ),
            {"sn": real_serial},
        ).fetchone()
        if existing:
            continue

        pppoe_user = (csv_row.get("Username") or "").strip()
        pppoe_pass = (csv_row.get("Password") or "").strip()

        if dry_run:
            logger.info(
                "  [DRY RUN] %s → %s (PPPoE:%s)",
                hw_serial,
                real_serial,
                pppoe_user or "none",
            )
        else:
            updates = {"sn": real_serial, "oid": ont_id}
            update_sql = "UPDATE ont_units SET serial_number = :sn"
            if pppoe_user:
                update_sql += ", pppoe_username = :user"
                updates["user"] = pppoe_user
            if pppoe_pass:
                update_sql += ", pppoe_password = :pass"
                updates["pass"] = pppoe_pass
            update_sql += " WHERE id = :oid"
            db.execute(text(update_sql), updates)
        stats.serials_fixed += 1
        if pppoe_user:
            stats.pppoe_filled += 1
        # Remove from csv_by_serial so we don't create a duplicate in phase 2
        csv_by_serial.pop(real_serial.upper(), None)

    if not dry_run and stats.serials_fixed:
        db.commit()
    logger.info(
        "  Fixed %d synthetic serials, filled %d PPPoE",
        stats.serials_fixed,
        stats.pppoe_filled,
    )

    # Phase 2: Create ONT records for CSV entries not in our DB
    # Re-read after phase 1 commits to include freshly fixed serials
    existing_serials = {
        r[0].upper()
        for r in db.execute(text("SELECT serial_number FROM ont_units")).fetchall()
        if r[0]
    }

    for sn_upper, csv_row in csv_by_serial.items():
        if sn_upper in existing_serials:
            continue

        real_serial = (csv_row.get("SN") or "").strip()
        olt_csv = (csv_row.get("OLT") or "").strip()
        dotmac_olt = _OLT_NAME_MAP.get(olt_csv, olt_csv)
        olt_id = olt_id_by_name.get(dotmac_olt)
        board = (csv_row.get("Board") or "").strip()
        port_str = (csv_row.get("Port") or "").strip()
        onu_type = (csv_row.get("Onu Type") or "").strip()
        pppoe_user = (csv_row.get("Username") or "").strip()
        pppoe_pass = (csv_row.get("Password") or "").strip()
        name = (csv_row.get("Name") or "").strip()

        if dry_run:
            logger.info(
                "  [DRY RUN] Would create ONT %s (%s, PPPoE:%s)",
                real_serial,
                onu_type,
                pppoe_user or "none",
            )
        else:
            import uuid

            new_id = str(uuid.uuid4())
            display_name = name or real_serial
            db.execute(
                text("""
                INSERT INTO ont_units (id, serial_number, vendor, model, olt_device_id,
                    board, port, pppoe_username, pppoe_password, name, address_or_comment,
                    is_active, pon_type, created_at, updated_at)
                VALUES (:id, :sn, 'Huawei', :model, :olt_id,
                    :board, :port, :user, :pass, :name, :addr,
                    true, 'gpon', NOW(), NOW())
            """),
                {
                    "id": new_id,
                    "sn": real_serial,
                    "model": onu_type or None,
                    "olt_id": olt_id,
                    "board": f"0/{board}" if board else None,
                    "port": port_str or None,
                    "user": pppoe_user or None,
                    "pass": pppoe_pass or None,
                    "name": display_name,
                    "addr": display_name,
                },
            )
            # Create an active assignment (no subscriber yet — step 2 will link)
            db.execute(
                text("""
                INSERT INTO ont_assignments (id, ont_unit_id, active, created_at, updated_at)
                VALUES (:id, :ont_id, true, NOW(), NOW())
            """),
                {"id": str(uuid.uuid4()), "ont_id": new_id},
            )
        stats.onts_created += 1

    if not dry_run and stats.onts_created:
        db.commit()
    logger.info("  Created %d new ONT records from CSV", stats.onts_created)


# ── Step 1: Sync GenieACS → DB ──────────────────────────────────────────────


def step1_sync_genieacs(
    db, acs_server_id: str, *, dry_run: bool, stats: MigrationStats
) -> None:
    """Sync GenieACS device list into tr069_cpe_devices and auto-link to ONTs."""
    from app.models.tr069 import Tr069AcsServer
    from app.services.genieacs import GenieACSClient
    from app.services.tr069 import CpeDevices

    logger.info("=" * 60)
    logger.info("Step 1: Sync GenieACS → tr069_cpe_devices")
    logger.info("=" * 60)

    server = db.get(Tr069AcsServer, acs_server_id)
    if not server:
        logger.error("ACS server %s not found", acs_server_id)
        return

    if dry_run:
        client = GenieACSClient(server.base_url)
        devices = client.list_devices()
        stats.genieacs_devices = len(devices)
        logger.info(
            "  [DRY RUN] Would sync %d GenieACS devices", stats.genieacs_devices
        )
        return

    result = CpeDevices.sync_from_genieacs(db, acs_server_id)
    stats.genieacs_devices = result.get("total", 0)
    stats.cpe_created = result.get("created", 0)
    stats.cpe_updated = result.get("updated", 0)
    stats.cpe_auto_linked = result.get("auto_linked", 0)
    logger.info(
        "  Synced: %d total, %d created, %d updated, %d auto-linked",
        stats.genieacs_devices,
        stats.cpe_created,
        stats.cpe_updated,
        stats.cpe_auto_linked,
    )


# ── Step 2: Link subscribers via PPPoE username ─────────────────────────────


def step2_link_subscribers(
    db,
    csv_path: str | None,
    *,
    dry_run: bool,
    stats: MigrationStats,
) -> None:
    """Link ONT assignments to subscribers by matching PPPoE username → access_credentials.

    Data sources (in priority order):
    1. ONT PPPoE username already in DB (from previous SmartOLT sync)
    2. SmartOLT CSV file (for any missing PPPoE credentials)
    """
    from app.models.network import OntAssignment, OntUnit

    logger.info("=" * 60)
    logger.info("Step 2: Link subscriber assignments via PPPoE")
    logger.info("=" * 60)

    # Load SmartOLT CSV if provided — use it to fill missing PPPoE creds
    csv_pppoe_by_serial: dict[str, tuple[str, str]] = {}
    if csv_path and Path(csv_path).exists():
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                sn = _normalize_serial(row.get("SN"))
                username = str(row.get("Username") or "").strip()
                password = str(row.get("Password") or "").strip()
                if sn and username and password:
                    csv_pppoe_by_serial[sn] = (username, password)
        logger.info("  Loaded %d PPPoE entries from CSV", len(csv_pppoe_by_serial))

    # Get all active ONTs with assignments
    onts_with_assignments = (
        db.query(OntUnit, OntAssignment)
        .join(OntAssignment, OntAssignment.ont_unit_id == OntUnit.id)
        .filter(
            OntUnit.is_active.is_(True),
            OntAssignment.active.is_(True),
        )
        .all()
    )
    logger.info("  Processing %d ONT assignments", len(onts_with_assignments))

    # Fill missing PPPoE from CSV
    csv_filled = 0
    for ont, _assignment in onts_with_assignments:
        if ont.pppoe_username:
            continue
        normalized_sn = _normalize_serial(ont.serial_number)
        csv_entry = csv_pppoe_by_serial.get(normalized_sn)
        if csv_entry:
            username, password = csv_entry
            if not dry_run:
                ont.pppoe_username = username
                ont.pppoe_password = password
            csv_filled += 1
    if csv_filled:
        if not dry_run:
            db.flush()
        logger.info("  Filled %d missing PPPoE credentials from CSV", csv_filled)

    # Build PPPoE → subscriber_id lookup from access_credentials
    pppoe_to_subscriber = dict(
        db.execute(
            text("""
                SELECT ac.username, ac.subscriber_id
                FROM access_credentials ac
                WHERE ac.is_active = true AND ac.username IS NOT NULL
            """)
        ).fetchall()
    )
    logger.info("  Loaded %d PPPoE → subscriber mappings", len(pppoe_to_subscriber))

    # Link assignments
    for ont, assignment in onts_with_assignments:
        if assignment.subscriber_id is not None:
            stats.assignments_already_linked += 1
            continue

        pppoe = ont.pppoe_username
        if not pppoe:
            stats.assignments_no_match += 1
            continue

        subscriber_id = pppoe_to_subscriber.get(pppoe)
        if not subscriber_id:
            stats.assignments_no_match += 1
            continue

        if dry_run:
            logger.info(
                "  [DRY RUN] Would link %s (PPPoE:%s) → subscriber %s",
                ont.serial_number,
                pppoe,
                str(subscriber_id)[:8],
            )
        else:
            assignment.subscriber_id = subscriber_id
        stats.assignments_linked += 1

    if not dry_run and stats.assignments_linked:
        db.commit()

    logger.info(
        "  Linked: %d, already linked: %d, no match: %d",
        stats.assignments_linked,
        stats.assignments_already_linked,
        stats.assignments_no_match,
    )


# ── Step 3: Push PPPoE + WiFi via TR-069 ────────────────────────────────────


def step3_push_config_via_tr069(
    db,
    acs_server_id: str,
    *,
    dry_run: bool,
    skip_push: bool,
    wifi_ssid_prefix: str,
    batch_pause: float,
    stats: MigrationStats,
) -> None:
    """Push PPPoE credentials and WiFi SSID to matched devices via TR-069."""
    from app.models.network import OntUnit
    from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
    from app.services.credential_crypto import decrypt_credential
    from app.services.genieacs import GenieACSClient, GenieACSError
    from app.services.network.ont_action_common import (
        TR069_ROOT_DEVICE,
        TR069_ROOT_IGD,
        build_tr069_params,
    )

    logger.info("=" * 60)
    logger.info("Step 3: Push PPPoE + WiFi config via TR-069")
    logger.info("=" * 60)

    if skip_push:
        logger.info("  --skip-tr069-push flag set, skipping")
        return

    server = db.get(Tr069AcsServer, acs_server_id)
    if not server:
        logger.error("ACS server %s not found", acs_server_id)
        return

    # Get ONTs linked to GenieACS that have PPPoE credentials
    linked_devices = (
        db.query(Tr069CpeDevice, OntUnit)
        .join(OntUnit, Tr069CpeDevice.ont_unit_id == OntUnit.id)
        .filter(
            Tr069CpeDevice.acs_server_id == acs_server_id,
            Tr069CpeDevice.is_active.is_(True),
            OntUnit.is_active.is_(True),
            OntUnit.pppoe_username.isnot(None),
            OntUnit.pppoe_username != "",
        )
        .all()
    )
    logger.info("  Found %d devices with PPPoE to push", len(linked_devices))

    # Build subscriber name lookup for WiFi SSID
    subscriber_names: dict[str, str] = {}
    sub_ids = set()
    for _cpe, ont in linked_devices:
        for assignment in getattr(ont, "assignments", []):
            if assignment.active and assignment.subscriber_id:
                sub_ids.add(str(assignment.subscriber_id))
    if sub_ids:
        subs = db.execute(
            text(
                "SELECT id, display_name, first_name, last_name FROM subscribers WHERE id = ANY(:ids)"
            ),
            {"ids": list(sub_ids)},
        ).fetchall()
        for sid, display, first, last in subs:
            name = display or f"{first or ''} {last or ''}".strip() or None
            if name:
                subscriber_names[str(sid)] = name

    client = GenieACSClient(server.base_url)

    for i, (cpe_dev, ont) in enumerate(linked_devices, 1):
        serial = ont.serial_number or "unknown"
        pppoe_user = ont.pppoe_username
        pppoe_pass = (
            decrypt_credential(ont.pppoe_password) if ont.pppoe_password else ""
        )

        if not pppoe_user or not pppoe_pass:
            stats.pppoe_skipped += 1
            continue

        if dry_run:
            logger.info(
                "  [DRY RUN] [%d/%d] %s → PPPoE:%s, WiFi:%s",
                i,
                len(linked_devices),
                serial,
                pppoe_user,
                _build_wifi_ssid(ont, subscriber_names, wifi_ssid_prefix),
            )
            stats.pppoe_skipped += 1
            stats.wifi_skipped += 1
            continue

        # Resolve GenieACS device ID
        device_id = _resolve_genieacs_device_id(client, cpe_dev, ont)
        if not device_id:
            logger.warning(
                "  [%d/%d] No GenieACS device for %s — skipping",
                i,
                len(linked_devices),
                serial,
            )
            stats.pppoe_skipped += 1
            stats.wifi_skipped += 1
            continue

        # Detect and cache data model
        root = _detect_and_cache_data_model(db, client, device_id, ont)
        if root:
            stats.data_model_cached += 1
        root = root or TR069_ROOT_IGD

        # ── Push PPPoE credentials ──
        if root == TR069_ROOT_DEVICE:
            pppoe_params = {
                "Device.PPP.Interface.1.Username": pppoe_user,
                "Device.PPP.Interface.1.Password": pppoe_pass,
            }
        else:
            pppoe_params = {
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Username": pppoe_user,
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password": pppoe_pass,
            }
        try:
            client.set_parameter_values(
                device_id, pppoe_params, connection_request=False
            )
            stats.pppoe_pushed += 1
            logger.info(
                "  [%d/%d] PPPoE pushed to %s (user:%s)",
                i,
                len(linked_devices),
                serial,
                pppoe_user,
            )
        except GenieACSError as exc:
            stats.pppoe_failed += 1
            stats.errors.append(f"PPPoE failed on {serial}: {exc}")
            logger.warning(
                "  [%d/%d] PPPoE push failed on %s: %s",
                i,
                len(linked_devices),
                serial,
                exc,
            )

        # ── Push WiFi SSID ──
        ssid = _build_wifi_ssid(ont, subscriber_names, wifi_ssid_prefix)
        if ssid:
            wifi_params = (
                build_tr069_params(
                    root,
                    {
                        "LANDevice.1.WLANConfiguration.1.SSID": ssid,
                    },
                )
                if root == TR069_ROOT_IGD
                else {
                    "Device.WiFi.SSID.1.SSID": ssid,
                }
            )
            try:
                client.set_parameter_values(
                    device_id, wifi_params, connection_request=False
                )
                stats.wifi_pushed += 1
            except GenieACSError as exc:
                stats.wifi_failed += 1
                stats.errors.append(f"WiFi SSID failed on {serial}: {exc}")
        else:
            stats.wifi_skipped += 1

        # ── Tag device ──
        try:
            client.add_tag(device_id, "migrated")
            stats.tagged += 1
        except GenieACSError:
            pass

        if batch_pause > 0 and i < len(linked_devices):
            time.sleep(batch_pause)


def _build_wifi_ssid(
    ont,
    subscriber_names: dict[str, str],
    prefix: str,
) -> str | None:
    """Build a WiFi SSID from subscriber name or ONT serial."""
    # Find subscriber name from active assignment
    for assignment in getattr(ont, "assignments", []):
        if assignment.active and assignment.subscriber_id:
            name = subscriber_names.get(str(assignment.subscriber_id))
            if name:
                # Sanitize: max 32 chars, alphanumeric + spaces + hyphens
                clean = re.sub(r"[^\w\s\-]", "", name).strip()[:28]
                if clean:
                    return f"{prefix}{clean}"
    return None


# ── Step 4: Update provisioning status ──────────────────────────────────────


def step4_update_provisioning_status(
    db,
    acs_server_id: str,
    *,
    dry_run: bool,
    stats: MigrationStats,
) -> None:
    """Mark matched ONTs as provisioned."""
    from app.models.network import OntUnit
    from app.models.tr069 import Tr069CpeDevice

    logger.info("=" * 60)
    logger.info("Step 4: Update provisioning status")
    logger.info("=" * 60)

    linked_onts = (
        db.query(OntUnit)
        .join(Tr069CpeDevice, Tr069CpeDevice.ont_unit_id == OntUnit.id)
        .filter(
            Tr069CpeDevice.acs_server_id == acs_server_id,
            Tr069CpeDevice.is_active.is_(True),
            OntUnit.is_active.is_(True),
        )
        .all()
    )

    now = datetime.now(UTC)
    for ont in linked_onts:
        current_status = getattr(ont, "provisioning_status", None)
        current_value = current_status.value if current_status else None

        if current_value == "provisioned":
            stats.already_provisioned += 1
            continue

        if dry_run:
            logger.info(
                "  [DRY RUN] Would mark %s as provisioned (was: %s)",
                ont.serial_number,
                current_value,
            )
            stats.provisioned += 1
            continue

        ont.provisioning_status = "provisioned"
        ont.last_provisioned_at = now
        if not ont.tr069_acs_server_id:
            ont.tr069_acs_server_id = acs_server_id
        stats.provisioned += 1

    if not dry_run and stats.provisioned:
        db.commit()

    logger.info(
        "  Marked %d as provisioned (%d already provisioned)",
        stats.provisioned,
        stats.already_provisioned,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _resolve_genieacs_device_id(client, cpe_dev, ont) -> str | None:
    """Resolve GenieACS device ID from CPE or ONT serial number."""
    from app.services.genieacs import GenieACSError

    serial = ont.serial_number or cpe_dev.serial_number
    if not serial:
        return None

    candidates: list[str] = [serial]
    normalized = _normalize_serial(serial)
    if normalized not in candidates:
        candidates.append(normalized)
    # Huawei display serial → hex (HWTC7D4701C3 → 485754437D4701C3)
    if len(normalized) == 12 and normalized[:4].isalpha():
        vendor_hex = normalized[:4].encode("ascii").hex().upper()
        candidates.append(vendor_hex + normalized[4:])
    # CPE serial if different
    cpe_serial = str(cpe_dev.serial_number or "").strip()
    if cpe_serial and cpe_serial not in candidates:
        candidates.append(cpe_serial)

    for candidate in candidates:
        try:
            devices = client.list_devices(
                query={"_id": {"$regex": f".*-{re.escape(candidate)}$"}}
            )
            if devices:
                device_id = str(devices[0].get("_id") or "").strip()
                if device_id:
                    return device_id
        except GenieACSError:
            continue
    return None


def _detect_and_cache_data_model(db, client, device_id: str, ont) -> str | None:
    """Detect TR-098 vs TR-181 data model and cache on ONT."""
    from app.services.genieacs import GenieACSError
    from app.services.network.ont_action_common import TR069_ROOT_DEVICE, TR069_ROOT_IGD

    if ont.tr069_data_model in (TR069_ROOT_DEVICE, TR069_ROOT_IGD):
        return ont.tr069_data_model

    try:
        device = client.get_device(device_id)
        root = (
            TR069_ROOT_DEVICE
            if isinstance(device.get("Device"), dict)
            else TR069_ROOT_IGD
        )
        ont.tr069_data_model = root
        db.flush()
        return root
    except GenieACSError as exc:
        logger.debug("Could not detect data model for %s: %s", ont.serial_number, exc)
        return None


# ── Summary ──────────────────────────────────────────────────────────────────


def print_summary(stats: MigrationStats) -> None:
    """Print final migration summary."""
    print("\n" + "=" * 64)
    print("  SmartOLT → GenieACS Migration Summary")
    print("=" * 64)

    print("\n  Step 0 — Serial Fix + CSV Import")
    print(f"    Serials fixed (HW→real):   {stats.serials_fixed}")
    print(f"    New ONTs created:          {stats.onts_created}")
    print(f"    PPPoE filled from CSV:     {stats.pppoe_filled}")

    print("\n  Step 1 — GenieACS Sync")
    print(f"    Devices in GenieACS:       {stats.genieacs_devices}")
    print(f"    CPE records created:       {stats.cpe_created}")
    print(f"    CPE records updated:       {stats.cpe_updated}")
    print(f"    Auto-linked to ONTs:       {stats.cpe_auto_linked}")

    print("\n  Step 2 — Subscriber Linkage")
    print(f"    Assignments linked:        {stats.assignments_linked}")
    print(f"    Already linked:            {stats.assignments_already_linked}")
    print(f"    No PPPoE match:            {stats.assignments_no_match}")

    print("\n  Step 3 — TR-069 Config Push")
    print(f"    PPPoE pushed:              {stats.pppoe_pushed}")
    print(f"    PPPoE failed:              {stats.pppoe_failed}")
    print(f"    PPPoE skipped:             {stats.pppoe_skipped}")
    print(f"    WiFi SSID pushed:          {stats.wifi_pushed}")
    print(f"    WiFi failed:               {stats.wifi_failed}")
    print(f"    WiFi skipped:              {stats.wifi_skipped}")
    print(f"    Data model cached:         {stats.data_model_cached}")
    print(f"    Tagged 'migrated':         {stats.tagged}")

    print("\n  Step 4 — Provisioning Status")
    print(f"    Marked provisioned:        {stats.provisioned}")
    print(f"    Already provisioned:       {stats.already_provisioned}")

    if stats.errors:
        print(f"\n  Errors ({len(stats.errors)}):")
        for err in stats.errors[:20]:
            print(f"    - {err}")
        if len(stats.errors) > 20:
            print(f"    ... and {len(stats.errors) - 20} more")

    print("=" * 64)


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate SmartOLT ONTs to GenieACS with full customer linkage"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Preview without changes")
    mode.add_argument("--execute", action="store_true", help="Apply changes")

    parser.add_argument(
        "--csv",
        default=None,
        help="SmartOLT CSV export path (fills missing PPPoE from CSV)",
    )
    parser.add_argument(
        "--skip-tr069-push",
        action="store_true",
        help="Skip PPPoE + WiFi push to devices (steps 1,2,4 only)",
    )
    parser.add_argument(
        "--only-step",
        type=int,
        choices=[0, 1, 2, 3, 4],
        default=None,
        help="Run only a specific step (0=serial fix, 1=sync, 2=link, 3=push, 4=status)",
    )
    parser.add_argument(
        "--wifi-ssid-prefix",
        default="DotMac-",
        help="WiFi SSID prefix before subscriber name (default: DotMac-)",
    )
    parser.add_argument(
        "--batch-pause",
        type=float,
        default=0.3,
        help="Pause between TR-069 pushes in seconds (default: 0.3)",
    )
    args = parser.parse_args()
    dry_run = args.dry_run

    if dry_run:
        logger.info("=== DRY RUN MODE — no changes will be made ===")
    else:
        logger.info("=== EXECUTE MODE — changes will be applied ===")

    # Auto-discover CSV if not provided
    csv_path = args.csv
    if not csv_path:
        csvs = sorted(Path(".").glob("SmartOLT_onus_list_*.csv"))
        if csvs:
            csv_path = str(csvs[-1])
            logger.info("Auto-discovered SmartOLT CSV: %s", csv_path)

    with dotmac_session() as db:
        try:
            from app.models.tr069 import Tr069AcsServer

            server = (
                db.query(Tr069AcsServer)
                .filter(Tr069AcsServer.is_active.is_(True))
                .first()
            )
            if not server:
                logger.error("No active ACS server found. Register one first.")
                sys.exit(1)

            acs_server_id = str(server.id)
            logger.info("Using ACS server: %s (%s)", server.name, server.base_url)

            stats = MigrationStats()
            only = args.only_step

            if only is None or only == 0:
                step0_fix_serials_and_import(db, csv_path, dry_run=dry_run, stats=stats)

            if only is None or only == 1:
                step1_sync_genieacs(db, acs_server_id, dry_run=dry_run, stats=stats)

            if only is None or only == 2:
                step2_link_subscribers(db, csv_path, dry_run=dry_run, stats=stats)

            if only is None or only == 3:
                step3_push_config_via_tr069(
                    db,
                    acs_server_id,
                    dry_run=dry_run,
                    skip_push=args.skip_tr069_push,
                    wifi_ssid_prefix=args.wifi_ssid_prefix,
                    batch_pause=args.batch_pause,
                    stats=stats,
                )

            if only is None or only == 4:
                step4_update_provisioning_status(
                    db, acs_server_id, dry_run=dry_run, stats=stats
                )

            print_summary(stats)

        except Exception:
            logger.exception("Migration failed")
            db.rollback()
            sys.exit(1)


if __name__ == "__main__":
    main()
