"""OLT hardware auto-discovery via SNMP Entity MIB.

Walks the standard Entity MIB (RFC 6933) to discover shelves, line cards,
card ports, power supplies, and fan units.  Updates the OLT device record
with firmware/software versions from sysDescr.
"""

from __future__ import annotations

import logging
import re
import subprocess  # nosec
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.network import (
    HardwareUnitStatus,
    OltCard,
    OltCardPort,
    OLTDevice,
    OltFanUnit,
    OltPortType,
    OltPowerUnit,
    OltShelf,
)
from app.services.credential_crypto import decrypt_credential

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Entity MIB OIDs (.1.3.6.1.2.1.47.1.1.1.1) ──────────────────────
_ENT_BASE = ".1.3.6.1.2.1.47.1.1.1.1"
_ENT_DESCR = f"{_ENT_BASE}.2"
_ENT_CONTAINED_IN = f"{_ENT_BASE}.4"
_ENT_CLASS = f"{_ENT_BASE}.5"
_ENT_NAME = f"{_ENT_BASE}.7"
_ENT_HW_REV = f"{_ENT_BASE}.8"
_ENT_FW_REV = f"{_ENT_BASE}.9"
_ENT_SERIAL = f"{_ENT_BASE}.11"
_ENT_MODEL = f"{_ENT_BASE}.13"

# entPhysicalClass values (RFC 6933 §4)
_CLASS_CHASSIS = "3"
_CLASS_POWER_SUPPLY = "6"
_CLASS_FAN = "7"
_CLASS_MODULE = "9"
_CLASS_PORT = "10"

# Huawei board temperature OID
_HW_BOARD_TEMP = ".1.3.6.1.4.1.2011.6.128.1.1.1.2.1.5"

# ── Slot/port parsing ────────────────────────────────────────────────
_FSP_RE = re.compile(r"(\d+)/(\d+)(?:/(\d+))?")
_SLOT_RE = re.compile(r"(?:slot|board)\s*(\d+)", re.IGNORECASE)
_PORT_RE = re.compile(r"(?:port|interface)\s*(\d+)", re.IGNORECASE)


@dataclass
class _PhysEntity:
    """Parsed row from the Entity MIB."""

    index: str
    descr: str = ""
    contained_in: str = ""
    phys_class: str = ""
    name: str = ""
    hw_rev: str = ""
    fw_rev: str = ""
    sw_rev: str = ""
    serial: str = ""
    model: str = ""


@dataclass
class _DiscoveryStats:
    shelves_created: int = 0
    shelves_updated: int = 0
    cards_created: int = 0
    cards_updated: int = 0
    ports_created: int = 0
    ports_updated: int = 0
    power_units_created: int = 0
    power_units_updated: int = 0
    fans_created: int = 0
    fans_updated: int = 0
    olt_updated: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "shelves_created": self.shelves_created,
            "shelves_updated": self.shelves_updated,
            "cards_created": self.cards_created,
            "cards_updated": self.cards_updated,
            "ports_created": self.ports_created,
            "ports_updated": self.ports_updated,
            "power_units_created": self.power_units_created,
            "power_units_updated": self.power_units_updated,
            "fans_created": self.fans_created,
            "fans_updated": self.fans_updated,
            "olt_updated": self.olt_updated,
            "errors": self.errors,
        }


# ── SNMP helpers ─────────────────────────────────────────────────────


def _build_snmp_target(olt: OLTDevice) -> SimpleNamespace | None:
    """Build a SimpleNamespace with SNMP credentials from the OLT record."""
    host = olt.mgmt_ip or olt.hostname
    if not host:
        return None
    raw_ro = olt.snmp_ro_community
    if not raw_ro or not raw_ro.strip():
        return None
    return SimpleNamespace(
        mgmt_ip=olt.mgmt_ip,
        hostname=olt.hostname,
        snmp_enabled=True,
        snmp_community=raw_ro.strip(),
        snmp_version=olt.snmp_version or "v2c",
        snmp_port=olt.snmp_port,
        vendor=olt.vendor,
    )


def _run_snmp_walk(
    target: SimpleNamespace,
    oid: str,
    *,
    timeout: int = 30,
    bulk: bool = True,
) -> list[str]:
    """Run an SNMP walk/bulkwalk and return raw output lines."""
    host = target.mgmt_ip or target.hostname
    if not host:
        raise RuntimeError("Missing SNMP host")
    if target.snmp_port:
        host = f"{host}:{target.snmp_port}"

    version = (target.snmp_version or "v2c").lower()
    if version not in {"v2c", "2c"}:
        raise RuntimeError(f"Only SNMP v2c supported, got {version}")

    community = (
        decrypt_credential(target.snmp_community)
        if target.snmp_community
        else ""
    )
    if not community:
        raise RuntimeError("SNMP community not configured")

    cmd = "snmpbulkwalk" if bulk else "snmpwalk"
    args = [cmd, "-v2c", "-c", community, "-OQn", host, oid]
    result = subprocess.run(  # noqa: S603
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


def _parse_entity_walk(lines: list[str], base_oid: str) -> dict[str, str]:
    """Parse SNMP walk output into {entity_index: value} dict.

    Uses -OQn output format: .full.oid.index = value
    """
    parsed: dict[str, str] = {}
    prefix = base_oid.rstrip(".")
    for line in lines:
        if " = " not in line:
            continue
        oid_part, value_part = line.split(" = ", 1)
        oid_part = oid_part.strip()
        if not oid_part.startswith(prefix):
            continue
        # Extract index after the base OID
        suffix = oid_part[len(prefix) :]
        if suffix.startswith("."):
            suffix = suffix[1:]
        if not suffix:
            continue
        value = value_part.strip().strip('"')
        if value.lower().startswith("no such"):
            continue
        parsed[suffix] = value
    return parsed


def _walk_entity_table(
    target: SimpleNamespace,
    oid: str,
    *,
    timeout: int = 30,
) -> dict[str, str]:
    """Walk a single Entity MIB column and return parsed results."""
    try:
        lines = _run_snmp_walk(target, oid, timeout=timeout, bulk=True)
        return _parse_entity_walk(lines, oid)
    except RuntimeError as exc:
        logger.warning("Entity MIB walk failed for %s: %s", oid, exc)
        return {}


def _extract_int(value: str) -> int | None:
    """Extract first integer from an SNMP value string."""
    match = re.search(r"(\d+)", value)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


# ── Entity tree building ─────────────────────────────────────────────


def _build_entity_tree(target: SimpleNamespace) -> list[_PhysEntity]:
    """Walk all Entity MIB columns and merge into a list of entities."""
    descr = _walk_entity_table(target, _ENT_DESCR)
    contained_in = _walk_entity_table(target, _ENT_CONTAINED_IN)
    phys_class = _walk_entity_table(target, _ENT_CLASS)
    name = _walk_entity_table(target, _ENT_NAME)
    hw_rev = _walk_entity_table(target, _ENT_HW_REV)
    fw_rev = _walk_entity_table(target, _ENT_FW_REV)
    serial = _walk_entity_table(target, _ENT_SERIAL)
    model = _walk_entity_table(target, _ENT_MODEL)

    if not phys_class:
        logger.info("Entity MIB not supported or empty — no entPhysicalClass data")
        return []

    all_indexes = set(phys_class.keys())
    entities: list[_PhysEntity] = []
    for idx in sorted(all_indexes, key=lambda x: int(x) if x.isdigit() else x):
        cls_val = _extract_int(phys_class.get(idx, ""))
        entities.append(
            _PhysEntity(
                index=idx,
                descr=descr.get(idx, ""),
                contained_in=contained_in.get(idx, ""),
                phys_class=str(cls_val) if cls_val is not None else "",
                name=name.get(idx, ""),
                hw_rev=hw_rev.get(idx, ""),
                fw_rev=fw_rev.get(idx, ""),
                serial=serial.get(idx, ""),
                model=model.get(idx, ""),
            )
        )

    return entities


def _parse_shelf_number(entity: _PhysEntity) -> int:
    """Extract shelf/frame number from entity name or default to 0."""
    # Try "Frame 0" / "Shelf 1" patterns
    match = re.search(r"(?:frame|shelf|chassis)\s*(\d+)", entity.name, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Try F/S/P pattern
    fsp = _FSP_RE.search(entity.name)
    if fsp:
        return int(fsp.group(1))
    return 0


def _parse_slot_number(entity: _PhysEntity) -> int | None:
    """Extract slot number from entity name."""
    # Try "Board 0/2" → slot 2, or "Slot 3"
    fsp = _FSP_RE.search(entity.name)
    if fsp:
        return int(fsp.group(2))
    match = _SLOT_RE.search(entity.name)
    if match:
        return int(match.group(1))
    # Try bare number at end of name
    match = re.search(r"(\d+)\s*$", entity.name)
    if match:
        return int(match.group(1))
    return None


def _parse_port_number(entity: _PhysEntity) -> int | None:
    """Extract port number from entity name."""
    # Try "0/2/3" → port 3
    fsp = _FSP_RE.search(entity.name)
    if fsp and fsp.group(3) is not None:
        return int(fsp.group(3))
    match = _PORT_RE.search(entity.name)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+)\s*$", entity.name)
    if match:
        return int(match.group(1))
    return None


_UPLINK_RE = re.compile(
    r"uplink|10ge|\bxge\b|\bge\b|gigabitethernet|tengigabit", re.IGNORECASE
)


def _classify_port_type(entity: _PhysEntity) -> OltPortType:
    """Determine port type from entity description/name."""
    text = f"{entity.descr} {entity.name}".lower()
    if "gpon" in text or "xgs-pon" in text or "epon" in text or "pon" in text:
        return OltPortType.pon
    if _UPLINK_RE.search(text):
        return OltPortType.uplink
    if "mgmt" in text or "management" in text or "meth" in text:
        return OltPortType.mgmt
    if "eth" in text:
        return OltPortType.ethernet
    # PON ports are the most common on OLTs — safe default
    return OltPortType.pon


def _slot_label(entity: _PhysEntity) -> str:
    """Build a slot label for power/fan units."""
    if entity.name:
        return entity.name.strip()[:40]
    if entity.descr:
        return entity.descr.strip()[:40]
    return f"Unit {entity.index}"


# ── Main discovery logic ─────────────────────────────────────────────


def discover_olt_hardware(
    db: Session,
    olt: OLTDevice,
) -> tuple[bool, str, dict[str, object]]:
    """Discover hardware inventory from an OLT via SNMP Entity MIB.

    Args:
        db: Database session.
        olt: The OLT device to discover.

    Returns:
        Tuple of (success, message, stats_dict).
    """
    target = _build_snmp_target(olt)
    if not target:
        return False, "No SNMP credentials configured", {}

    stats = _DiscoveryStats()

    # Probe reachability with sysDescr
    try:
        sys_lines = _run_snmp_walk(target, ".1.3.6.1.2.1.1.1.0", bulk=False, timeout=10)
    except RuntimeError as exc:
        return False, f"OLT unreachable via SNMP: {exc}", {}

    # Update OLT system info from sysDescr
    _update_olt_system_info(db, olt, sys_lines)
    stats.olt_updated = True

    # Walk Entity MIB
    entities = _build_entity_tree(target)
    if not entities:
        return True, "Entity MIB empty or unsupported", stats.to_dict()

    # Build parent→children map for hierarchy resolution
    parent_map: dict[str, _PhysEntity] = {e.index: e for e in entities}

    # Classify entities
    chassis_entities = [e for e in entities if e.phys_class == _CLASS_CHASSIS]
    module_entities = [e for e in entities if e.phys_class == _CLASS_MODULE]
    port_entities = [e for e in entities if e.phys_class == _CLASS_PORT]
    psu_entities = [e for e in entities if e.phys_class == _CLASS_POWER_SUPPLY]
    fan_entities = [e for e in entities if e.phys_class == _CLASS_FAN]

    logger.info(
        "OLT %s Entity MIB: %d chassis, %d modules, %d ports, %d PSUs, %d fans",
        olt.name,
        len(chassis_entities),
        len(module_entities),
        len(port_entities),
        len(psu_entities),
        len(fan_entities),
    )

    # Upsert shelves
    shelf_by_number = _upsert_shelves(db, olt, chassis_entities, stats)

    # Ensure at least shelf 0 exists for cards
    if not shelf_by_number:
        shelf_by_number = _ensure_default_shelf(db, olt)

    # Upsert line cards
    card_by_key = _upsert_cards(
        db, olt, module_entities, shelf_by_number, parent_map, stats
    )

    # Upsert card ports
    _upsert_card_ports(db, port_entities, card_by_key, parent_map, stats)

    # Upsert power supplies
    _upsert_power_units(db, olt, psu_entities, stats)

    # Upsert fan units
    _upsert_fan_units(db, olt, fan_entities, stats)

    # Optional: Huawei board temperature
    vendor_text = (olt.vendor or "").lower()
    if "huawei" in vendor_text:
        _update_huawei_temperatures(db, olt, target, card_by_key)

    db.commit()

    total_created = (
        stats.shelves_created
        + stats.cards_created
        + stats.ports_created
        + stats.power_units_created
        + stats.fans_created
    )
    total_updated = (
        stats.shelves_updated
        + stats.cards_updated
        + stats.ports_updated
        + stats.power_units_updated
        + stats.fans_updated
    )
    msg = f"Discovered {total_created} new, updated {total_updated} existing"
    return True, msg, stats.to_dict()


def _update_olt_system_info(
    db: Session, olt: OLTDevice, sys_lines: list[str]
) -> None:
    """Update OLT firmware/software version from sysDescr."""
    if not sys_lines:
        return
    sys_descr = ""
    for line in sys_lines:
        if " = " in line:
            sys_descr = line.split(" = ", 1)[1].strip().strip('"')
            break
    if not sys_descr:
        return

    # Extract version from common formats:
    # "Huawei Versatile Routing Platform Software VRP (R) V800R021C10SPC100"
    # "ZTE ZXA10 C300 Version: V2.0.1"
    ver_match = re.search(
        r"(?:Version[:\s]*|VRP\s*\(R\)\s*)(V[\w.]+)", sys_descr, re.IGNORECASE
    )
    if ver_match:
        version = ver_match.group(1)
        if olt.software_version != version:
            olt.software_version = version
            logger.info("Updated OLT %s software_version to %s", olt.name, version)

    # Extract firmware from "Software Version V800R021C10SPC100"
    fw_match = re.search(r"(V\d+R\d+C\d+\w*)", sys_descr)
    if fw_match:
        fw = fw_match.group(1)
        if olt.firmware_version != fw:
            olt.firmware_version = fw

    db.flush()


def _upsert_shelves(
    db: Session,
    olt: OLTDevice,
    chassis_entities: list[_PhysEntity],
    stats: _DiscoveryStats,
) -> dict[int, OltShelf]:
    """Upsert OltShelf records from chassis entities."""
    existing = {
        s.shelf_number: s
        for s in db.scalars(
            select(OltShelf).where(OltShelf.olt_id == olt.id)
        ).all()
    }

    result: dict[int, OltShelf] = dict(existing)
    for entity in chassis_entities:
        shelf_num = _parse_shelf_number(entity)
        shelf = existing.get(shelf_num)
        if shelf:
            _update_shelf_fields(shelf, entity)
            stats.shelves_updated += 1
        else:
            shelf = OltShelf(
                olt_id=olt.id,
                shelf_number=shelf_num,
                label=entity.name or entity.descr or f"Shelf {shelf_num}",
                serial_number=entity.serial or None,
                status=HardwareUnitStatus.active,
                is_active=True,
            )
            db.add(shelf)
            db.flush()
            stats.shelves_created += 1
        result[shelf_num] = shelf

    return result


def _update_shelf_fields(shelf: OltShelf, entity: _PhysEntity) -> None:
    """Update mutable fields on an existing shelf."""
    if entity.serial and entity.serial.strip():
        shelf.serial_number = entity.serial.strip()
    if entity.name:
        shelf.label = entity.name.strip()
    shelf.status = HardwareUnitStatus.active


def _ensure_default_shelf(db: Session, olt: OLTDevice) -> dict[int, OltShelf]:
    """Ensure shelf 0 exists as a fallback container."""
    existing = db.scalars(
        select(OltShelf).where(OltShelf.olt_id == olt.id, OltShelf.shelf_number == 0)
    ).first()
    if existing:
        return {0: existing}
    shelf = OltShelf(
        olt_id=olt.id,
        shelf_number=0,
        label="Default Shelf",
        status=HardwareUnitStatus.active,
        is_active=True,
    )
    db.add(shelf)
    db.flush()
    return {0: shelf}


def _upsert_cards(
    db: Session,
    olt: OLTDevice,
    module_entities: list[_PhysEntity],
    shelf_by_number: dict[int, OltShelf],
    parent_map: dict[str, _PhysEntity],
    stats: _DiscoveryStats,
) -> dict[tuple[int, int], OltCard]:
    """Upsert OltCard records from module entities.

    Returns:
        Dict keyed by (shelf_number, slot_number) → OltCard.
    """
    # Load existing cards keyed by (shelf_number, slot_number)
    existing_cards: dict[tuple[int, int], OltCard] = {}
    for s_num, s_obj in shelf_by_number.items():
        cards_in_shelf = db.scalars(
            select(OltCard).where(OltCard.shelf_id == s_obj.id)
        ).all()
        for c in cards_in_shelf:
            existing_cards[(s_num, c.slot_number)] = c

    result: dict[tuple[int, int], OltCard] = dict(existing_cards)
    for entity in module_entities:
        slot_num = _parse_slot_number(entity)
        if slot_num is None:
            continue

        # Determine parent shelf from containedIn
        shelf_num = _resolve_shelf_number(entity, parent_map)
        resolved_shelf = shelf_by_number.get(shelf_num)
        if not resolved_shelf:
            # Fall back to shelf 0
            resolved_shelf = shelf_by_number.get(0)
            shelf_num = 0
        if not resolved_shelf:
            stats.errors.append(f"No shelf for card {entity.name} (entity {entity.index})")
            continue

        key = (shelf_num, slot_num)
        existing_card = existing_cards.get(key)
        if existing_card:
            _update_card_fields(existing_card, entity)
            stats.cards_updated += 1
            result[key] = existing_card
        else:
            new_card = OltCard(
                shelf_id=resolved_shelf.id,
                slot_number=slot_num,
                card_type=_classify_card_type(entity),
                model=entity.model or entity.descr or None,
                serial_number=entity.serial or None,
                hardware_version=entity.hw_rev or None,
                firmware_version=entity.fw_rev or None,
                status=HardwareUnitStatus.active,
                is_active=True,
            )
            db.add(new_card)
            db.flush()
            stats.cards_created += 1
            result[key] = new_card

    return result


def _resolve_shelf_number(
    entity: _PhysEntity, parent_map: dict[str, _PhysEntity]
) -> int:
    """Walk up the containedIn chain to find the parent chassis shelf number."""
    visited: set[str] = set()
    current = entity
    for _ in range(10):  # max depth guard
        parent_idx = current.contained_in
        parent_int = _extract_int(parent_idx)
        if parent_int is None:
            break
        parent_key = str(parent_int)
        if parent_key in visited:
            break
        visited.add(parent_key)
        parent = parent_map.get(parent_key)
        if not parent:
            break
        if parent.phys_class == _CLASS_CHASSIS:
            return _parse_shelf_number(parent)
        current = parent
    return 0


def _update_card_fields(card: OltCard, entity: _PhysEntity) -> None:
    """Update mutable fields on an existing card."""
    if entity.model:
        card.model = entity.model
    elif entity.descr:
        card.model = entity.descr
    if entity.serial and entity.serial.strip():
        card.serial_number = entity.serial.strip()
    if entity.hw_rev:
        card.hardware_version = entity.hw_rev
    if entity.fw_rev:
        card.firmware_version = entity.fw_rev
    card.status = HardwareUnitStatus.active
    if not card.card_type:
        card.card_type = _classify_card_type(entity)


def _classify_card_type(entity: _PhysEntity) -> str | None:
    """Classify card type from entity description."""
    text = f"{entity.descr} {entity.name} {entity.model}".lower()
    if "gpon" in text or "gpbd" in text or "gpbh" in text or "gpfd" in text:
        return "GPON"
    if "xgs" in text or "xgpon" in text:
        return "XGS-PON"
    if "epon" in text or "epbd" in text:
        return "EPON"
    if "uplink" in text or "gicf" in text or "gicg" in text:
        return "Uplink"
    if "control" in text or "scun" in text or "scuh" in text or "mpla" in text:
        return "Control"
    if "power" in text or "pwr" in text:
        return "Power"
    return None


def _upsert_card_ports(
    db: Session,
    port_entities: list[_PhysEntity],
    card_by_key: dict[tuple[int, int], OltCard],
    parent_map: dict[str, _PhysEntity],
    stats: _DiscoveryStats,
) -> None:
    """Upsert OltCardPort records from port entities."""
    # Build lookup: card_id → {port_number: OltCardPort}
    all_card_ids = [c.id for c in card_by_key.values()]
    existing_ports: dict[tuple[str, int], OltCardPort] = {}
    if all_card_ids:
        ports = db.scalars(
            select(OltCardPort).where(OltCardPort.card_id.in_(all_card_ids))
        ).all()
        for p in ports:
            existing_ports[(str(p.card_id), p.port_number)] = p

    for entity in port_entities:
        port_num = _parse_port_number(entity)
        if port_num is None:
            continue

        # Find parent card via containedIn chain
        parent_idx = _extract_int(entity.contained_in)
        if parent_idx is None:
            continue
        parent = parent_map.get(str(parent_idx))
        if not parent or parent.phys_class != _CLASS_MODULE:
            continue

        slot_num = _parse_slot_number(parent)
        if slot_num is None:
            continue

        shelf_num = _resolve_shelf_number(parent, parent_map)
        card = card_by_key.get((shelf_num, slot_num))
        if not card:
            continue

        key = (str(card.id), port_num)
        port = existing_ports.get(key)
        if port:
            port.status = HardwareUnitStatus.active
            if entity.name:
                port.name = entity.name.strip()[:120]
            port_type = _classify_port_type(entity)
            if port_type != port.port_type:
                port.port_type = port_type
            stats.ports_updated += 1
        else:
            port = OltCardPort(
                card_id=card.id,
                port_number=port_num,
                name=entity.name or None,
                port_type=_classify_port_type(entity),
                status=HardwareUnitStatus.active,
                is_active=True,
            )
            db.add(port)
            stats.ports_created += 1

    db.flush()


def _upsert_power_units(
    db: Session,
    olt: OLTDevice,
    psu_entities: list[_PhysEntity],
    stats: _DiscoveryStats,
) -> None:
    """Upsert OltPowerUnit records from power supply entities."""
    existing = {
        pu.slot: pu
        for pu in db.scalars(
            select(OltPowerUnit).where(OltPowerUnit.olt_id == olt.id)
        ).all()
    }

    for entity in psu_entities:
        slot = _slot_label(entity)
        pu = existing.get(slot)
        if pu:
            pu.status = HardwareUnitStatus.active
            stats.power_units_updated += 1
        else:
            pu = OltPowerUnit(
                olt_id=olt.id,
                slot=slot,
                status=HardwareUnitStatus.active,
                is_active=True,
            )
            db.add(pu)
            stats.power_units_created += 1

    db.flush()


def _upsert_fan_units(
    db: Session,
    olt: OLTDevice,
    fan_entities: list[_PhysEntity],
    stats: _DiscoveryStats,
) -> None:
    """Upsert OltFanUnit records from fan entities."""
    existing = {
        fu.slot: fu
        for fu in db.scalars(
            select(OltFanUnit).where(OltFanUnit.olt_id == olt.id)
        ).all()
    }

    for entity in fan_entities:
        slot = _slot_label(entity)
        fu = existing.get(slot)
        if fu:
            fu.status = HardwareUnitStatus.active
            if entity.name:
                fu.label = entity.name.strip()[:120]
            stats.fans_updated += 1
        else:
            fu = OltFanUnit(
                olt_id=olt.id,
                slot=slot,
                label=entity.name or entity.descr or None,
                status=HardwareUnitStatus.active,
                is_active=True,
            )
            db.add(fu)
            stats.fans_created += 1

    db.flush()


def _update_huawei_temperatures(
    db: Session,
    olt: OLTDevice,
    target: SimpleNamespace,
    card_by_key: dict[tuple[int, int], OltCard],
) -> None:
    """Read Huawei board temperature OIDs and update card records."""
    try:
        temp_data = _walk_entity_table(target, _HW_BOARD_TEMP)
    except Exception as exc:
        logger.warning("Huawei temperature walk failed for OLT %s: %s", olt.name, exc)
        return

    if not temp_data:
        return

    for idx, value in temp_data.items():
        temp_val = _extract_int(value)
        if temp_val is None:
            continue
        # Huawei index is typically frame.slot: "0.2"
        parts = idx.split(".")
        if len(parts) >= 2:
            try:
                shelf_num = int(parts[0])
                slot_num = int(parts[1])
            except ValueError:
                continue
            card = card_by_key.get((shelf_num, slot_num))
            if card:
                card.temperature = float(temp_val)

    db.flush()
