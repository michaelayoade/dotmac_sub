"""Sync SmartOLT data into DotMac Sub — enriches existing records, fills gaps.

Covers: ONU types, speed profiles, zones, VLANs, ONT enrichment,
subscriber-ONT linking, signal data, GPS, management IPs.

Usage:
    poetry run python scripts/migration/smartolt_sync.py --dry-run
    poetry run python scripts/migration/smartolt_sync.py --execute
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import requests
from sqlalchemy import text

from scripts.migration.db_connections import dotmac_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("smartolt_sync")

# ── SmartOLT API config ─────────────────────────────────────────────────────

SMARTOLT_API_URL = os.environ.get(
    "SMARTOLT_API_URL", "https://dotmac.smartolt.com/api"
)
SMARTOLT_API_KEY = os.environ.get("SMARTOLT_API_KEY", "")
if not SMARTOLT_API_KEY:
    logger.warning(
        "SMARTOLT_API_KEY not set — set via env var or .env file. "
        "API calls will fail."
    )

HEADERS = {"X-Token": SMARTOLT_API_KEY}


def api_get(endpoint: str, *, retries: int = 3) -> dict[str, Any]:
    """Call a SmartOLT API endpoint and return parsed JSON."""
    import time

    url = f"{SMARTOLT_API_URL}/{endpoint}"
    for attempt in range(retries):
        resp = requests.get(url, headers=HEADERS, timeout=60)
        if resp.status_code == 403 and attempt < retries - 1:
            wait = 5 * (attempt + 1)
            logger.warning("403 on %s, retrying in %ds...", endpoint, wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return resp.json()  # unreachable but satisfies type checker


# ── OLT name + board mapping ────────────────────────────────────────────────

# SmartOLT OLT ID -> DotMac OLT name
OLT_NAME_MAP: dict[str, str] = {
    "1": "Karsana Huawei OLT",
    "3": "Garki Huawei OLT",
    "4": "BOI Huawei OLT",
    "6": "Jabi Huawei OLT",
    "7": "Gudu Huawei OLT",
    "10": "Gwarimpa Huawei OLT",
    "11": "SPDC Huawei OLT",
}

# SmartOLT board number -> DB frame/slot notation per OLT
# (derived from comparing SNMP-discovered board values with SmartOLT board values)
BOARD_MAP: dict[str, dict[str, str]] = {
    "1": {"1": "0/2"},  # Karsana MA5608T — slot 2
    "3": {"2": "0/2"},  # Garki MA5800-X2 — slot 2
    "4": {"1": "0/2"},  # BOI MA5608T — slot 2
    "6": {"1": "0/2"},  # Jabi MA5608T — slot 2
    "7": {"0": "0/0", "1": "0/2"},  # Gudu MA5600 — two service boards
    "10": {"2": "0/2"},  # Gwarimpa MA5800-X2 — slot 2
    "11": {"1": "0/2"},  # SPDC MA5608T — slot 2
}


# ── Stats tracker ────────────────────────────────────────────────────────────


class Stats:
    def __init__(self) -> None:
        self.zones_created = 0
        self.onu_types_created = 0
        self.speed_profiles_created = 0
        self.vlans_created = 0
        self.vlans_updated = 0
        self.onts_updated = 0
        self.onts_created = 0
        self.onts_skipped = 0
        self.assignments_linked = 0
        self.assignments_created = 0
        self.pon_ports_created = 0
        self.errors: list[str] = []

    def summary(self) -> str:
        lines = [
            "─── SmartOLT Sync Summary ────────────────────────",
            f"  Zones created:         {self.zones_created}",
            f"  ONU types created:     {self.onu_types_created}",
            f"  Speed profiles created: {self.speed_profiles_created}",
            f"  VLANs created:         {self.vlans_created}",
            f"  VLANs updated:         {self.vlans_updated}",
            f"  ONTs updated:          {self.onts_updated}",
            f"  ONTs created:          {self.onts_created}",
            f"  ONTs skipped:          {self.onts_skipped}",
            f"  PON ports created:     {self.pon_ports_created}",
            f"  Assignments linked:    {self.assignments_linked}",
            f"  Assignments created:   {self.assignments_created}",
            f"  Errors:                {len(self.errors)}",
            "──────────────────────────────────────────────────",
        ]
        if self.errors:
            lines.append("  Errors:")
            for e in self.errors[:20]:
                lines.append(f"    - {e}")
        return "\n".join(lines)


# ── Phase 1: Zones ──────────────────────────────────────────────────────────


def sync_zones(db: Any, stats: Stats, *, dry_run: bool) -> dict[str, str]:
    """Create network_zones from SmartOLT zones. Returns {smartolt_id: uuid}."""
    logger.info("Fetching zones from SmartOLT...")
    cache_path = Path("/tmp/smartolt_zones.json")  # noqa: S108
    if cache_path.exists():
        with open(cache_path) as f:
            data = json.load(f)
    else:
        data = api_get("onu/get_zones")
        with open(cache_path, "w") as f:
            json.dump(data, f)
    zones = data.get("response", [])
    logger.info("SmartOLT zones: %d", len(zones))

    # Load existing zones by name
    existing = {}
    for row in db.execute(text("SELECT id, name FROM network_zones")):
        existing[row.name] = str(row.id)

    zone_map: dict[str, str] = {}
    for z in zones:
        so_id = z["id"]
        name = z["name"]
        if name in existing:
            zone_map[so_id] = existing[name]
            continue

        zone_uuid = str(uuid.uuid4())
        zone_map[so_id] = zone_uuid
        if not dry_run:
            db.execute(
                text(
                    "INSERT INTO network_zones (id, name, is_active, created_at, updated_at) "
                    "VALUES (:id, :name, true, now(), now())"
                ),
                {"id": zone_uuid, "name": name},
            )
        stats.zones_created += 1
        logger.info("  Zone: %s -> %s", name, zone_uuid)

    return zone_map


# ── Phase 2: ONU Types ──────────────────────────────────────────────────────


def sync_onu_types(
    db: Any, smartolt_onus: list[dict], stats: Stats, *, dry_run: bool
) -> dict[str, str]:
    """Create onu_types from SmartOLT ONU type data. Returns {smartolt_type_id: uuid}."""
    logger.info("Extracting ONU types from SmartOLT data...")

    # Collect unique types with port counts from the first ONU of each type
    type_info: dict[str, dict] = {}
    for o in smartolt_onus:
        tid = o["onu_type_id"]
        if tid not in type_info:
            eth_ports = len(o.get("ethernet_ports") or [])
            wifi_count = len(o.get("wifi_ports") or [])
            voip_count = len(o.get("voip_ports") or [])
            catv = 1 if o.get("catv") and "supported" in str(o["catv"]).lower() else 0
            type_info[tid] = {
                "name": o["onu_type_name"],
                "ethernet_ports": eth_ports or 4,  # default 4 if not reported
                "wifi_ports": wifi_count or 1,
                "voip_ports": voip_count,
                "catv_ports": catv,
            }

    # Load existing by name
    existing = {}
    for row in db.execute(text("SELECT id, name FROM onu_types")):
        existing[row.name] = str(row.id)

    type_map: dict[str, str] = {}
    for tid, info in type_info.items():
        name = info["name"]
        if name in existing:
            type_map[tid] = existing[name]
            continue

        type_uuid = str(uuid.uuid4())
        type_map[tid] = type_uuid
        if not dry_run:
            db.execute(
                text(
                    "INSERT INTO onu_types "
                    "(id, name, pon_type, ethernet_ports, wifi_ports, voip_ports, "
                    "catv_ports, is_active, created_at, updated_at) "
                    "VALUES (:id, :name, 'gpon', :eth, :wifi, :voip, :catv, "
                    "true, now(), now())"
                ),
                {
                    "id": type_uuid,
                    "name": name,
                    "eth": info["ethernet_ports"],
                    "wifi": info["wifi_ports"],
                    "voip": info["voip_ports"],
                    "catv": info["catv_ports"],
                },
            )
        stats.onu_types_created += 1
        logger.info(
            "  ONU type: %s (eth=%d wifi=%d voip=%d catv=%d)",
            name,
            info["ethernet_ports"],
            info["wifi_ports"],
            info["voip_ports"],
            info["catv_ports"],
        )

    return type_map


# ── Phase 3: Speed Profiles ─────────────────────────────────────────────────


def sync_speed_profiles(
    db: Any, smartolt_onus: list[dict], stats: Stats, *, dry_run: bool
) -> dict[str, str]:
    """Create speed_profiles from SmartOLT service port data. Returns {name: uuid}."""
    logger.info("Extracting speed profiles from SmartOLT data...")

    profile_names: set[str] = set()
    for o in smartolt_onus:
        for sp in o.get("service_ports") or []:
            for key in ("upload_speed", "download_speed"):
                name = sp.get(key, "")
                if name and name != "MANAGEMENT":
                    profile_names.add(name)

    existing = {}
    for row in db.execute(text("SELECT id, name FROM speed_profiles")):
        existing[row.name] = str(row.id)

    profile_map: dict[str, str] = {}
    for name in sorted(profile_names):
        if name in existing:
            profile_map[name] = existing[name]
            continue

        # Try to parse speed from name (e.g. "SPL_22_70Mbps_Fiber_DOWN")
        speed_kbps = _parse_speed_kbps(name)
        direction = "download" if "DOWN" in name.upper() else "upload"

        profile_uuid = str(uuid.uuid4())
        profile_map[name] = profile_uuid
        if not dry_run:
            db.execute(
                text(
                    "INSERT INTO speed_profiles "
                    "(id, name, direction, speed_kbps, is_active, created_at, updated_at) "
                    "VALUES (:id, :name, :direction, :speed_kbps, true, now(), now())"
                ),
                {
                    "id": profile_uuid,
                    "name": name,
                    "direction": direction,
                    "speed_kbps": speed_kbps,
                },
            )
        stats.speed_profiles_created += 1
        logger.info("  Speed profile: %s (%d kbps %s)", name, speed_kbps, direction)

    # Also add 1G and MANAGEMENT as reference
    for name, speed_kbps, direction in [
        ("1G", 1_000_000, "download"),
        ("MANAGEMENT", 0, "download"),
    ]:
        if name not in existing and name not in profile_map:
            profile_uuid = str(uuid.uuid4())
            profile_map[name] = profile_uuid
            if not dry_run:
                db.execute(
                    text(
                        "INSERT INTO speed_profiles "
                        "(id, name, direction, speed_kbps, is_active, created_at, updated_at) "
                        "VALUES (:id, :name, :direction, :speed_kbps, true, now(), now())"
                    ),
                    {
                        "id": profile_uuid,
                        "name": name,
                        "direction": direction,
                        "speed_kbps": speed_kbps,
                    },
                )
            stats.speed_profiles_created += 1

    return profile_map


def _parse_speed_kbps(name: str) -> int:
    """Extract speed in kbps from a profile name like 'SPL_22_70Mbps_Fiber_DOWN'."""
    import re

    m = re.search(r"(\d+)\s*[Mm]bps", name)
    if m:
        return int(m.group(1)) * 1000
    m = re.search(r"(\d+)\s*[Gg]bps", name)
    if m:
        return int(m.group(1)) * 1_000_000
    m = re.search(r"(\d+)\s*[Kk]bps", name)
    if m:
        return int(m.group(1))
    if name == "1G":
        return 1_000_000
    return 0


# ── Phase 4: VLANs ──────────────────────────────────────────────────────────


def sync_vlans(
    db: Any, olt_uuid_map: dict[str, str], stats: Stats, *, dry_run: bool
) -> dict[str, str]:
    """Create VLANs from SmartOLT per-OLT VLAN data. Returns {olt_uuid:vlan_tag: uuid}."""
    logger.info("Fetching VLANs from SmartOLT per OLT...")

    # Ensure a default region_zone exists (VLANs require region_id NOT NULL)
    default_region_id = None
    row = db.execute(text("SELECT id FROM region_zones LIMIT 1")).fetchone()
    if row:
        default_region_id = str(row.id)
    else:
        default_region_id = str(uuid.uuid4())
        if not dry_run:
            db.execute(
                text(
                    "INSERT INTO region_zones (id, name, code, is_active, created_at, updated_at) "
                    "VALUES (:id, 'Default Region', 'default', true, now(), now())"
                ),
                {"id": default_region_id},
            )
        logger.info("  Created default region_zone: %s", default_region_id)

    # Existing VLANs keyed by (region_id, tag) — unique constraint
    existing_by_tag: dict[int, str] = {}
    for row in db.execute(text("SELECT id, tag FROM vlans")):
        existing_by_tag[row.tag] = str(row.id)

    vlan_map: dict[str, str] = {}  # "olt_uuid:tag" -> vlan_uuid

    for so_olt_id, dotmac_name in OLT_NAME_MAP.items():
        olt_uuid = olt_uuid_map.get(dotmac_name)
        if not olt_uuid:
            continue

        vlan_cache = Path(f"/tmp/smartolt_vlans_{so_olt_id}.json")  # noqa: S108
        try:
            if vlan_cache.exists():
                with open(vlan_cache) as f:
                    data = json.load(f)
            else:
                data = api_get(f"olt/get_vlans/{so_olt_id}")
                with open(vlan_cache, "w") as f:
                    json.dump(data, f)
        except Exception as e:
            logger.warning("Failed to fetch VLANs for OLT %s: %s", so_olt_id, e)
            continue

        for v in data.get("response", []):
            tag = int(v["vlan"])
            scope = v.get("scope", "")
            desc = v.get("description") or ""

            # Map scope to purpose (enum: internet, management, tr069, iptv, voip, other)
            purpose = "internet"
            if "mgmt" in scope:
                purpose = "management"
            elif "voip" in scope:
                purpose = "voip"
            elif "lan_to_lan" in scope:
                purpose = "other"

            map_key = f"{olt_uuid}:{tag}"

            # VLAN tags are unique per region — reuse if same tag already exists
            if tag in existing_by_tag:
                vlan_map[map_key] = existing_by_tag[tag]
                # Update description/olt if missing
                if desc and not dry_run:
                    db.execute(
                        text(
                            "UPDATE vlans SET description = :desc, purpose = :purpose, "
                            "updated_at = now() WHERE id = :id AND "
                            "(description IS NULL OR description = '')"
                        ),
                        {"desc": desc, "purpose": purpose, "id": existing_by_tag[tag]},
                    )
                    stats.vlans_updated += 1
                continue

            vlan_uuid = str(uuid.uuid4())
            vlan_map[map_key] = vlan_uuid
            existing_by_tag[tag] = vlan_uuid  # track for dedup across OLTs
            if not dry_run:
                db.execute(
                    text(
                        "INSERT INTO vlans "
                        "(id, region_id, olt_device_id, tag, name, description, purpose, "
                        "is_active, created_at, updated_at) "
                        "VALUES (:id, :region_id, :olt_id, :tag, :name, :desc, :purpose, "
                        "true, now(), now())"
                    ),
                    {
                        "id": vlan_uuid,
                        "region_id": default_region_id,
                        "olt_id": olt_uuid,
                        "tag": tag,
                        "name": f"VLAN {tag}",
                        "desc": desc,
                        "purpose": purpose,
                    },
                )
            stats.vlans_created += 1
            logger.info(
                "  VLAN %d on %s (%s) -> %s", tag, dotmac_name, purpose, vlan_uuid
            )

    return vlan_map


# ── Phase 5: ONT enrichment + creation ──────────────────────────────────────

# Status mapping
STATUS_MAP = {
    "Online": "online",
    "Offline": "offline",
    "Power fail": "offline",
    "LOS": "offline",
    None: "unknown",
}

OFFLINE_REASON_MAP = {
    "Power fail": "power_fail",
    "LOS": "los",
    "Offline": None,
    "Online": None,
    None: None,
}

WAN_MODE_MAP = {
    "PPPoE": "pppoe",
    "DHCP": "dhcp",
    "Static IP": "static_ip",
    "Setup via ONU webpage": "setup_via_onu",
    "": None,
}

ONU_MODE_MAP = {
    "Routing": "routing",
    "Bridging": "bridging",
}


def sync_onts(
    db: Any,
    smartolt_onus: list[dict],
    olt_uuid_map: dict[str, str],
    zone_map: dict[str, str],
    type_map: dict[str, str],
    vlan_map: dict[str, str],
    stats: Stats,
    *,
    dry_run: bool,
) -> dict[str, str]:
    """Enrich existing ONTs and create new ones. Returns {smartolt_sn: ont_uuid}."""
    logger.info("Syncing %d ONTs from SmartOLT...", len(smartolt_onus))

    # Import credential encryption for PPPoE passwords
    try:
        from app.services.credential_crypto import encrypt_credential
    except ImportError:
        encrypt_credential = None  # type: ignore[assignment]
        logger.warning("credential_crypto not available — PPPoE passwords will be stored plaintext")

    # Build DB ONT index: (olt_uuid, db_board, port, onu_index) -> row
    db_ont_index: dict[tuple, dict] = {}
    rows = db.execute(
        text(
            "SELECT id, serial_number, name, board, port, olt_device_id "
            "FROM ont_units"
        )
    ).fetchall()
    for r in rows:
        try:
            onu_idx = int(r.name.split(":")[1])
            key = (str(r.olt_device_id), r.board, r.port, onu_idx)
            db_ont_index[key] = {
                "id": str(r.id),
                "serial_number": r.serial_number,
            }
        except (IndexError, ValueError):
            pass

    logger.info("DB ONT index: %d entries", len(db_ont_index))

    # Get TR069 ACS server ID (GenieACS)
    acs_row = db.execute(
        text("SELECT id FROM tr069_acs_servers WHERE is_active = true LIMIT 1")
    ).fetchone()
    acs_id = str(acs_row.id) if acs_row else None

    # Track PON ports: (olt_uuid, board, port) -> pon_port_uuid
    pon_port_index: dict[tuple, str] = {}
    for r in db.execute(
        text("SELECT id, olt_id, name FROM pon_ports")
    ).fetchall():
        # Parse name like "pon-0/2/4" -> (olt_id, "0/2", "4")
        parts = r.name.replace("pon-", "").rsplit("/", 1)
        if len(parts) == 2:
            pon_port_index[(str(r.olt_id), parts[0], parts[1])] = str(r.id)

    ont_map: dict[str, str] = {}  # smartolt_sn -> ont_uuid

    for o in smartolt_onus:
        so_olt_id = o["olt_id"]
        dotmac_name = OLT_NAME_MAP.get(so_olt_id)
        if not dotmac_name:
            stats.onts_skipped += 1
            continue

        olt_uuid = olt_uuid_map.get(dotmac_name)
        if not olt_uuid:
            stats.onts_skipped += 1
            continue

        board_map = BOARD_MAP.get(so_olt_id, {})
        db_board = board_map.get(o["board"])
        if not db_board:
            stats.onts_skipped += 1
            stats.errors.append(
                f"No board mapping for SO OLT {so_olt_id} board {o['board']}"
            )
            continue

        # Build common update fields from SmartOLT data
        zone_id = zone_map.get(o.get("zone_id") or "") if o.get("zone_id") else None
        onu_type_id = type_map.get(o.get("onu_type_id") or "") if o.get("onu_type_id") else None
        wan_mode = WAN_MODE_MAP.get(o.get("wan_mode", ""))
        onu_mode = ONU_MODE_MAP.get(o.get("mode", ""))
        online_status = STATUS_MAP.get(o.get("status"), "unknown")
        offline_reason = OFFLINE_REASON_MAP.get(o.get("status"))

        # Resolve VLAN UUIDs
        mgmt_vlan_tag = o.get("mgmt_ip_vlan")
        mgmt_vlan_id = None
        if mgmt_vlan_tag:
            mgmt_vlan_id = vlan_map.get(f"{olt_uuid}:{mgmt_vlan_tag}")

        # Parse signal values
        signal_1310 = _safe_float(o.get("signal_1310"))
        signal_1490 = _safe_float(o.get("signal_1490"))

        # GPS
        lat = _safe_float(o.get("latitude"))
        lon = _safe_float(o.get("longitude"))

        # Common field dict
        fields = {
            "zone_id": zone_id,
            "onu_type_id": onu_type_id,
            "onu_mode": onu_mode,
            "wan_mode": wan_mode,
            "online_status": online_status,
            "pppoe_username": o.get("username") or None,
            "pppoe_password": (
                encrypt_credential(o.get("password"))
                if encrypt_credential and o.get("password")
                else o.get("password") or None
            ),
            "name": o.get("name") or None,
            "address_or_comment": o.get("address") or None,
            "mgmt_ip_mode": _mgmt_ip_mode(o.get("mgmt_ip_mode", "")),
            "mgmt_ip_address": o.get("mgmt_ip_address") or None,
            "mgmt_vlan_id": mgmt_vlan_id,
            "voip_enabled": o.get("voip_service") == "Enabled",
            "onu_rx_signal_dbm": signal_1310,
            "olt_rx_signal_dbm": signal_1490,
            "gps_latitude": lat,
            "gps_longitude": lon,
            "use_gps": bool(lat and lon),
            "tr069_acs_server_id": acs_id if o.get("tr069") == "Enabled" else None,
        }
        if offline_reason:
            fields["offline_reason"] = offline_reason

        # Try to match existing DB ONT
        match_key = (olt_uuid, db_board, o["port"], int(o["onu"]))
        existing = db_ont_index.get(match_key)

        if existing:
            # UPDATE existing ONT — enrich with SmartOLT data
            ont_uuid = existing["id"]
            ont_map[o["sn"]] = ont_uuid

            if not dry_run:
                set_parts = []
                params: dict[str, Any] = {"ont_id": ont_uuid}
                for k, v in fields.items():
                    if v is not None:
                        set_parts.append(f"{k} = :{k}")
                        params[k] = v
                # Always update these
                set_parts.append("external_id = :external_id")
                params["external_id"] = f"smartolt:{o['sn']}"
                set_parts.append("updated_at = now()")

                if set_parts:
                    sql = f"UPDATE ont_units SET {', '.join(set_parts)} WHERE id = :ont_id"  # noqa: S608
                    db.execute(text(sql), params)

            stats.onts_updated += 1

        else:
            # CREATE new ONT
            ont_uuid = str(uuid.uuid4())
            ont_map[o["sn"]] = ont_uuid

            # Ensure PON port exists
            pon_key = (olt_uuid, db_board, o["port"])
            pon_port_id = pon_port_index.get(pon_key)
            if not pon_port_id and not dry_run:
                pon_port_id = str(uuid.uuid4())
                pon_name = f"pon-{db_board}/{o['port']}"
                db.execute(
                    text(
                        "INSERT INTO pon_ports (id, olt_id, name, is_active, created_at, updated_at) "
                        "VALUES (:id, :olt_id, :name, true, now(), now())"
                    ),
                    {"id": pon_port_id, "olt_id": olt_uuid, "name": pon_name},
                )
                pon_port_index[pon_key] = pon_port_id
                stats.pon_ports_created += 1

            if not dry_run:
                db.execute(
                    text(
                        "INSERT INTO ont_units "
                        "(id, serial_number, vendor, model, olt_device_id, "
                        "board, port, pon_type, external_id, is_active, "
                        "name, address_or_comment, zone_id, onu_type_id, "
                        "onu_mode, wan_mode, online_status, offline_reason, "
                        "pppoe_username, pppoe_password, "
                        "mgmt_ip_mode, mgmt_ip_address, mgmt_vlan_id, "
                        "voip_enabled, tr069_acs_server_id, "
                        "onu_rx_signal_dbm, olt_rx_signal_dbm, "
                        "gps_latitude, gps_longitude, use_gps, "
                        "created_at, updated_at) "
                        "VALUES "
                        "(:id, :sn, 'Huawei', :model, :olt_id, "
                        ":board, :port, 'gpon', :ext_id, true, "
                        ":name, :address, :zone_id, :onu_type_id, "
                        ":onu_mode, :wan_mode, :online_status, :offline_reason, "
                        ":pppoe_username, :pppoe_password, "
                        ":mgmt_ip_mode, :mgmt_ip_address, :mgmt_vlan_id, "
                        ":voip_enabled, :tr069_acs_server_id, "
                        ":onu_rx_signal_dbm, :olt_rx_signal_dbm, "
                        ":gps_latitude, :gps_longitude, :use_gps, "
                        "now(), now())"
                    ),
                    {
                        "id": ont_uuid,
                        "sn": o["sn"],
                        "model": o.get("onu_type_name") or "Unknown",
                        "olt_id": olt_uuid,
                        "board": db_board,
                        "port": o["port"],
                        "ext_id": f"smartolt:{o['sn']}",
                        "name": fields["name"],
                        "address": fields["address_or_comment"],
                        "zone_id": fields["zone_id"],
                        "onu_type_id": fields["onu_type_id"],
                        "onu_mode": fields["onu_mode"],
                        "wan_mode": fields["wan_mode"],
                        "online_status": fields["online_status"],
                        "offline_reason": fields.get("offline_reason"),
                        "pppoe_username": fields["pppoe_username"],
                        "pppoe_password": fields["pppoe_password"],
                        "mgmt_ip_mode": fields["mgmt_ip_mode"],
                        "mgmt_ip_address": fields["mgmt_ip_address"],
                        "mgmt_vlan_id": fields["mgmt_vlan_id"],
                        "voip_enabled": fields["voip_enabled"],
                        "tr069_acs_server_id": fields["tr069_acs_server_id"],
                        "onu_rx_signal_dbm": fields["onu_rx_signal_dbm"],
                        "olt_rx_signal_dbm": fields["olt_rx_signal_dbm"],
                        "gps_latitude": fields["gps_latitude"],
                        "gps_longitude": fields["gps_longitude"],
                        "use_gps": fields["use_gps"],
                    },
                )

            stats.onts_created += 1

    return ont_map


# ── Phase 6: Subscriber ↔ ONT linking ───────────────────────────────────────


def link_subscribers(
    db: Any,
    smartolt_onus: list[dict],
    ont_map: dict[str, str],
    olt_uuid_map: dict[str, str],
    stats: Stats,
    *,
    dry_run: bool,
) -> None:
    """Link ONTs to subscribers via PPPoE username matching."""
    logger.info("Linking subscribers to ONTs via PPPoE username...")

    # Build username -> (subscriber_id, credential_id) map from access_credentials
    cred_index: dict[str, dict] = {}
    rows = db.execute(
        text(
            "SELECT ac.id, ac.subscriber_id, ac.username "
            "FROM access_credentials ac "
            "WHERE ac.username IS NOT NULL"
        )
    ).fetchall()
    for r in rows:
        cred_index[r.username] = {
            "subscriber_id": str(r.subscriber_id),
            "credential_id": str(r.id),
        }
    logger.info("  Credential index: %d usernames", len(cred_index))

    # Get existing ONT assignments keyed by ont_unit_id
    existing_assignments: dict[str, dict] = {}
    for r in db.execute(
        text(
            "SELECT id, ont_unit_id, subscriber_id, active "
            "FROM ont_assignments"
        )
    ).fetchall():
        existing_assignments[str(r.ont_unit_id)] = {
            "id": str(r.id),
            "subscriber_id": str(r.subscriber_id) if r.subscriber_id else None,
            "active": r.active,
        }

    # Get PON port for each ONT
    ont_pon_ports: dict[str, str] = {}
    for r in db.execute(
        text(
            "SELECT oa.ont_unit_id, oa.pon_port_id FROM ont_assignments oa"
        )
    ).fetchall():
        ont_pon_ports[str(r.ont_unit_id)] = str(r.pon_port_id)

    # Also build a fallback PON port lookup from ont_units board/port
    ont_board_port: dict[str, tuple] = {}
    for r in db.execute(
        text("SELECT id, olt_device_id, board, port FROM ont_units")
    ).fetchall():
        ont_board_port[str(r.id)] = (str(r.olt_device_id), r.board, r.port)

    # Flush pending inserts so newly created PON ports are visible
    if not dry_run:
        db.flush()

    pon_port_index: dict[tuple, str] = {}
    for r in db.execute(text("SELECT id, olt_id, name FROM pon_ports")).fetchall():
        parts = r.name.replace("pon-", "").rsplit("/", 1)
        if len(parts) == 2:
            pon_port_index[(str(r.olt_id), parts[0], parts[1])] = str(r.id)

    for o in smartolt_onus:
        username = o.get("username")
        if not username:
            continue

        sn = o["sn"]
        ont_uuid = ont_map.get(sn)
        if not ont_uuid:
            continue

        cred = cred_index.get(username)
        if not cred:
            continue

        subscriber_id = cred["subscriber_id"]

        # Check existing assignment
        existing = existing_assignments.get(ont_uuid)
        if existing:
            if existing["subscriber_id"] == subscriber_id:
                continue  # Already linked correctly
            # Update existing assignment with subscriber
            if not dry_run:
                db.execute(
                    text(
                        "UPDATE ont_assignments SET subscriber_id = :sub_id, "
                        "active = true, updated_at = now() "
                        "WHERE id = :id"
                    ),
                    {"sub_id": subscriber_id, "id": existing["id"]},
                )
            stats.assignments_linked += 1
        else:
            # Create new assignment
            # Find PON port
            pon_port_id = None
            bp = ont_board_port.get(ont_uuid)
            if bp:
                pon_port_id = pon_port_index.get(bp)

            if not pon_port_id:
                stats.errors.append(
                    f"No PON port for ONT {sn} (uuid={ont_uuid})"
                )
                continue

            if not dry_run:
                db.execute(
                    text(
                        "INSERT INTO ont_assignments "
                        "(id, ont_unit_id, pon_port_id, subscriber_id, "
                        "active, assigned_at, created_at, updated_at) "
                        "VALUES (:id, :ont_id, :pon_id, :sub_id, "
                        "true, now(), now(), now())"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "ont_id": ont_uuid,
                        "pon_id": pon_port_id,
                        "sub_id": subscriber_id,
                    },
                )
            stats.assignments_created += 1

    logger.info(
        "  Linked: %d updated, %d created",
        stats.assignments_linked,
        stats.assignments_created,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _safe_float(value: Any) -> float | None:
    """Parse a float, returning None for invalid/empty/blank values."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _mgmt_ip_mode(raw: str) -> str | None:
    """Map SmartOLT mgmt_ip_mode to DB enum value (inactive, static_ip, dhcp)."""
    mapping = {
        "Static IP": "static_ip",
        "DHCP": "dhcp",
        "Inactive": "inactive",
        "": None,
        None: None,
    }
    return mapping.get(raw)


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync SmartOLT data into DotMac Sub")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview changes only")
    group.add_argument("--execute", action="store_true", help="Apply changes to DB")
    parser.add_argument(
        "--cache", action="store_true", default=True,
        help="Use cached SmartOLT data from /tmp/smartolt_onus.json if available",
    )
    parser.add_argument(
        "--no-cache", action="store_false", dest="cache",
        help="Force fresh API fetch (ignore cache)",
    )
    args = parser.parse_args()

    dry_run = args.dry_run
    if dry_run:
        logger.info("=== DRY RUN — no database changes will be made ===")
    else:
        logger.info("=== EXECUTING — changes will be committed ===")

    stats = Stats()

    # Fetch all ONU data from SmartOLT (or use cached file)
    cache_path = "/tmp/smartolt_onus.json"  # noqa: S108
    if args.cache and Path(cache_path).exists():
        logger.info("Loading ONU data from cache: %s", cache_path)
        with open(cache_path) as f:
            onu_data = json.load(f)
    else:
        logger.info("Fetching all ONU details from SmartOLT API...")
        onu_data = api_get("onu/get_all_onus_details")
        # Cache for re-runs
        with open(cache_path, "w") as f:
            json.dump(onu_data, f)
        logger.info("Cached to %s", cache_path)

    smartolt_onus = onu_data.get("onus", [])
    logger.info("Loaded %d ONUs", len(smartolt_onus))

    with dotmac_session() as db:
        # Build OLT UUID map
        olt_uuid_map: dict[str, str] = {}
        for row in db.execute(text("SELECT id, name FROM olt_devices")):
            olt_uuid_map[row.name] = str(row.id)

        # Phase 1: Zones
        zone_map = sync_zones(db, stats, dry_run=dry_run)

        # Phase 2: ONU Types
        type_map = sync_onu_types(db, smartolt_onus, stats, dry_run=dry_run)

        # Phase 3: Speed Profiles
        sync_speed_profiles(db, smartolt_onus, stats, dry_run=dry_run)

        # Phase 4: VLANs
        vlan_map = sync_vlans(db, olt_uuid_map, stats, dry_run=dry_run)

        # Phase 5: ONT enrichment + creation
        ont_map = sync_onts(
            db,
            smartolt_onus,
            olt_uuid_map,
            zone_map,
            type_map,
            vlan_map,
            stats,
            dry_run=dry_run,
        )

        # Phase 6: Subscriber linking
        link_subscribers(
            db, smartolt_onus, ont_map, olt_uuid_map, stats, dry_run=dry_run
        )

        if not dry_run:
            db.commit()
            logger.info("Changes committed to database.")
        else:
            db.rollback()
            logger.info("Dry run complete — no changes applied.")

    print()
    print(stats.summary())


if __name__ == "__main__":
    main()
