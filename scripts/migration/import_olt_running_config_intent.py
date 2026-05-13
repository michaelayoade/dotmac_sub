"""Import ONT intent from saved Huawei OLT running-config files.

The importer is intentionally conservative. It parses local running-config
snapshots and updates existing ONT records with OLT-derived intent, but it does
not create subscribers, PPPoE credentials, or push anything to live devices.

Usage:
    poetry run python scripts/migration/import_olt_running_config_intent.py
    poetry run python scripts/migration/import_olt_running_config_intent.py --apply
"""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.network import (
    ConfigMethod,
    MgmtIpMode,
    OLTDevice,
    OntUnit,
    Vlan,
)

DEFAULT_CONFIG_DIR = Path("/root/dotmac-olt-configs/2026-04-17")


@dataclass
class ParsedOnt:
    olt_key: str
    file_path: str
    board: str
    port: str
    ont_index: str
    serial: str
    vendor_serial: str
    external_id: str
    line_profile_id: int | None = None
    service_profile_id: int | None = None
    description: str | None = None
    mgmt_ip_address: str | None = None
    mgmt_subnet_mask: str | None = None
    mgmt_vlan_tag: int | None = None
    mgmt_gateway: str | None = None
    mgmt_primary_dns: str | None = None
    mgmt_secondary_dns: str | None = None
    tr069_olt_profile_id: int | None = None
    pppoe_ip_index: int | None = None
    internet_config_ip_index: int | None = None
    wan_config_ip_index: int | None = None
    wan_config_profile_id: int | None = None
    service_ports: list[dict[str, Any]] = field(default_factory=list)

    @property
    def internet_ip_index(self) -> int | None:
        indices = [
            value
            for value in (
                self.pppoe_ip_index,
                self.internet_config_ip_index,
                self.wan_config_ip_index,
            )
            if value is not None
        ]
        if not indices or len(set(indices)) > 1:
            return None
        return indices[0]

    @property
    def internet_stack_validation_errors(self) -> list[str]:
        indices = [
            value
            for value in (
                self.pppoe_ip_index,
                self.internet_config_ip_index,
                self.wan_config_ip_index,
            )
            if value is not None
        ]
        errors: list[str] = []
        if len(set(indices)) > 1:
            errors.append(
                "Misaligned internet ip-index values: "
                f"pppoe={self.pppoe_ip_index}, "
                f"internet-config={self.internet_config_ip_index}, "
                f"wan-config={self.wan_config_ip_index}"
            )
        if self.pppoe_ip_index is not None and self.internet_config_ip_index is None:
            errors.append("PPPoE ipconfig exists without ont internet-config")
        if self.pppoe_ip_index is not None and self.wan_config_ip_index is None:
            errors.append("PPPoE ipconfig exists without ont wan-config")
        return errors

    @property
    def internet_stack_validation_status(self) -> str:
        return "invalid" if self.internet_stack_validation_errors else "valid"

    def snapshot(self) -> dict[str, Any]:
        return {
            "source": "olt_running_config_import",
            "source_file": self.file_path,
            "imported_at": datetime.now(UTC).isoformat(),
            "olt_key": self.olt_key,
            "board": self.board,
            "port": self.port,
            "ont_index": self.ont_index,
            "external_id": self.external_id,
            "serial": self.serial,
            "vendor_serial": self.vendor_serial,
            "line_profile_id": self.line_profile_id,
            "service_profile_id": self.service_profile_id,
            "description": self.description,
            "management": {
                "mode": "static" if self.mgmt_ip_address else None,
                "ip_address": self.mgmt_ip_address,
                "subnet_mask": self.mgmt_subnet_mask,
                "vlan": self.mgmt_vlan_tag,
                "gateway": self.mgmt_gateway,
                "primary_dns": self.mgmt_primary_dns,
                "secondary_dns": self.mgmt_secondary_dns,
            },
            "tr069_olt_profile_id": self.tr069_olt_profile_id,
            "internet_stack": {
                "internet_ip_index": self.internet_ip_index,
                "pppoe_ip_index": self.pppoe_ip_index,
                "internet_config_ip_index": self.internet_config_ip_index,
                "wan_config_ip_index": self.wan_config_ip_index,
                "wan_config_profile_id": self.wan_config_profile_id,
                "validation_status": self.internet_stack_validation_status,
                "validation_errors": self.internet_stack_validation_errors,
            },
            "service_ports": self.service_ports,
        }


def _clean_command_text(text: str) -> str:
    text = text.replace("\r", "\n")
    text = re.sub(r"\n\s+", "\n ", text)
    return text


def _commands_from_config(text: str) -> list[str]:
    commands: list[str] = []
    current: str | None = None
    starts = (
        "ont add ",
        "ont ipconfig ",
        "ont internet-config ",
        "ont wan-config ",
        "ont tr069-server-config ",
        "service-port ",
        "interface gpon ",
    )
    continuations = (
        "ont-srvprofile-id ",
        "priority ",
        "index ",
        "traffic-table ",
        "_",
    )
    for raw_line in _clean_command_text(text).splitlines():
        line = raw_line.strip()
        if not line or line == "#":
            continue
        if line.startswith(starts):
            if current:
                commands.append(current)
            current = line
            continue
        if current and line.startswith(continuations):
            current = f"{current} {line}"
            continue
        if current:
            commands.append(current)
            current = None
    if current:
        commands.append(current)
    return [re.sub(r"\s+", " ", command).strip() for command in commands]


def _human_huawei_serial(value: str) -> str:
    raw = re.sub(r"[^A-Fa-f0-9]", "", value or "").upper()
    if len(raw) >= 8:
        try:
            prefix = bytes.fromhex(raw[:8]).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            prefix = ""
        if prefix.isalpha():
            return prefix + raw[8:]
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def _normalized_serial(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _olt_key_from_path(path: Path) -> str:
    return path.stem.split("_", 1)[0].lower()


def parse_config(path: Path) -> list[ParsedOnt]:
    olt_key = _olt_key_from_path(path)
    commands = _commands_from_config(path.read_text(errors="replace"))
    current_board: str | None = None
    onts: dict[tuple[str, str], ParsedOnt] = {}

    for command in commands:
        interface_match = re.match(r"interface gpon (\d+/\d+)", command)
        if interface_match:
            current_board = interface_match.group(1)
            continue

        add_match = re.match(
            r'ont add (?P<port>\d+) (?P<ont>\d+) sn-auth "(?P<serial>[^"]+)" '
            r".*?ont-lineprofile-id (?P<line>\d+) .*?ont-srvprofile-id "
            r'(?P<srv>\d+)(?: desc "(?P<desc>.*)")?',
            command,
        )
        if add_match and current_board:
            port = add_match.group("port")
            ont_index = add_match.group("ont")
            vendor_serial = _normalized_serial(add_match.group("serial"))
            human_serial = _human_huawei_serial(vendor_serial)
            key = (port, ont_index)
            onts[key] = ParsedOnt(
                olt_key=olt_key,
                file_path=str(path),
                board=current_board,
                port=port,
                ont_index=ont_index,
                serial=human_serial,
                vendor_serial=vendor_serial,
                external_id=f"{current_board}/{port}.{ont_index}",
                line_profile_id=int(add_match.group("line")),
                service_profile_id=int(add_match.group("srv")),
                description=(add_match.group("desc") or "").strip() or None,
            )
            continue

        ip_match = re.match(
            r"ont ipconfig (?P<port>\d+) (?P<ont>\d+) static ip-address "
            r"(?P<ip>\S+) mask (?P<mask>\S+) vlan (?P<vlan>\d+).*?"
            r"(?:gateway (?P<gateway>\S+))?.*?"
            r"(?:pri-dns (?P<pri>\S+))?.*?"
            r"(?:slave-dns (?P<slave>\S+))?",
            command,
        )
        if ip_match:
            parsed = onts.get((ip_match.group("port"), ip_match.group("ont")))
            if parsed:
                parsed.mgmt_ip_address = ip_match.group("ip")
                parsed.mgmt_subnet_mask = ip_match.group("mask")
                parsed.mgmt_vlan_tag = int(ip_match.group("vlan"))
                parsed.mgmt_gateway = ip_match.group("gateway")
                parsed.mgmt_primary_dns = ip_match.group("pri")
                parsed.mgmt_secondary_dns = ip_match.group("slave")
            continue

        pppoe_match = re.match(
            r"ont ipconfig (?P<port>\d+) (?P<ont>\d+) ip-index "
            r"(?P<idx>\d+) pppoe\b",
            command,
        )
        if pppoe_match:
            parsed = onts.get((pppoe_match.group("port"), pppoe_match.group("ont")))
            if parsed:
                parsed.pppoe_ip_index = int(pppoe_match.group("idx"))
            continue

        internet_config_match = re.match(
            r"ont internet-config (?P<port>\d+) (?P<ont>\d+) ip-index "
            r"(?P<idx>\d+)",
            command,
        )
        if internet_config_match:
            parsed = onts.get(
                (
                    internet_config_match.group("port"),
                    internet_config_match.group("ont"),
                )
            )
            if parsed:
                parsed.internet_config_ip_index = int(
                    internet_config_match.group("idx")
                )
            continue

        wan_config_match = re.match(
            r"ont wan-config (?P<port>\d+) (?P<ont>\d+) ip-index "
            r"(?P<idx>\d+) profile-id (?P<profile>\d+)",
            command,
        )
        if wan_config_match:
            parsed = onts.get(
                (wan_config_match.group("port"), wan_config_match.group("ont"))
            )
            if parsed:
                parsed.wan_config_ip_index = int(wan_config_match.group("idx"))
                parsed.wan_config_profile_id = int(wan_config_match.group("profile"))
            continue

        tr069_match = re.match(
            r"ont tr069-server-config (?P<port>\d+) (?P<ont>\d+) profile-id "
            r"(?P<profile>\d+)",
            command,
        )
        if tr069_match:
            parsed = onts.get((tr069_match.group("port"), tr069_match.group("ont")))
            if parsed:
                parsed.tr069_olt_profile_id = int(tr069_match.group("profile"))
            continue

        service_match = re.match(
            r"service-port (?P<index>\d+) vlan (?P<vlan>\d+) gpon "
            r"(?P<pon>\d+/\d+/(?P<port>\d+)) ont (?P<ont>\d+) gemport "
            r"(?P<gem>\d+).*?user-vlan\s+(?P<user_vlan>\d+).*?"
            r"inbound traffic-table index (?P<inbound>\d+).*?"
            r"outbound traffic-table\s+index\s+(?P<outbound>\d+)",
            command,
        )
        if service_match:
            parsed = onts.get((service_match.group("port"), service_match.group("ont")))
            if parsed:
                parsed.service_ports.append(
                    {
                        "service_port": int(service_match.group("index")),
                        "vlan": int(service_match.group("vlan")),
                        "pon": service_match.group("pon"),
                        "gemport": int(service_match.group("gem")),
                        "user_vlan": int(service_match.group("user_vlan")),
                        "inbound_traffic_table": int(service_match.group("inbound")),
                        "outbound_traffic_table": int(service_match.group("outbound")),
                    }
                )

    return list(onts.values())


def _load_olt_map(db: Session) -> dict[str, OLTDevice]:
    olts = db.scalars(select(OLTDevice).where(OLTDevice.is_active.is_(True))).all()
    result: dict[str, OLTDevice] = {}
    for olt in olts:
        words = str(olt.name or "").lower().split()
        if words:
            result[words[0]] = olt
    return result


def _load_vlan_by_olt_tag(db: Session) -> dict[tuple[str, int], Vlan]:
    rows = db.scalars(select(Vlan).where(Vlan.is_active.is_(True))).all()
    result: dict[tuple[str, int], Vlan] = {}
    for vlan in rows:
        if vlan.olt_device_id is not None and vlan.tag is not None:
            result[(str(vlan.olt_device_id), int(vlan.tag))] = vlan
    return result


def _load_ont_by_serial(db: Session) -> dict[str, OntUnit]:
    onts = db.scalars(select(OntUnit).where(OntUnit.is_active.is_(True))).all()
    result: dict[str, OntUnit] = {}
    for ont in onts:
        for value in (ont.serial_number, ont.vendor_serial_number):
            normalized = _normalized_serial(value)
            if normalized:
                result[normalized] = ont
    return result


def import_configs(config_dir: Path, *, apply: bool = False) -> dict[str, int]:
    db = SessionLocal()
    try:
        olt_by_key = _load_olt_map(db)
        vlan_by_olt_tag = _load_vlan_by_olt_tag(db)
        ont_by_serial = _load_ont_by_serial(db)
        parsed_onts: list[ParsedOnt] = []
        for path in sorted(config_dir.glob("*.cfg")):
            parsed_onts.extend(parse_config(path))

        stats = {
            "files": len(list(config_dir.glob("*.cfg"))),
            "parsed_onts": len(parsed_onts),
            "matched_onts": 0,
            "unmatched_onts": 0,
            "updated_onts": 0,
            "vlan_links": 0,
            "invalid_internet_stacks": 0,
        }

        for parsed in parsed_onts:
            ont = ont_by_serial.get(
                _normalized_serial(parsed.serial)
            ) or ont_by_serial.get(_normalized_serial(parsed.vendor_serial))
            if ont is None:
                stats["unmatched_onts"] += 1
                continue
            stats["matched_onts"] += 1
            if parsed.internet_stack_validation_errors:
                stats["invalid_internet_stacks"] += 1
            olt = olt_by_key.get(parsed.olt_key)
            vlan = (
                vlan_by_olt_tag.get((str(olt.id), parsed.mgmt_vlan_tag))
                if olt is not None and parsed.mgmt_vlan_tag is not None
                else None
            )
            if not apply:
                continue

            changed = False
            if olt and ont.olt_device_id != olt.id:
                ont.olt_device_id = olt.id
                changed = True
            for attr, value in (
                ("serial_number", parsed.serial),
                ("vendor_serial_number", parsed.vendor_serial),
                ("external_id", parsed.external_id),
                ("board", parsed.board),
                ("port", parsed.port),
                ("address_or_comment", parsed.description),
                ("mgmt_ip_address", parsed.mgmt_ip_address),
                ("tr069_olt_profile_id", parsed.tr069_olt_profile_id),
            ):
                if value not in (None, "") and getattr(ont, attr) != value:
                    setattr(ont, attr, value)
                    changed = True
            if parsed.mgmt_ip_address and ont.mgmt_ip_mode != MgmtIpMode.static_ip:
                ont.mgmt_ip_mode = MgmtIpMode.static_ip
                changed = True
            if parsed.tr069_olt_profile_id is not None:
                ont.config_method = ConfigMethod.tr069
            if vlan and ont.mgmt_vlan_id != vlan.id:
                ont.mgmt_vlan_id = vlan.id
                stats["vlan_links"] += 1
                changed = True

            ont.tr069_last_snapshot = parsed.snapshot()
            ont.tr069_last_snapshot_at = datetime.now(UTC)
            changed = True

            if changed:
                stats["updated_onts"] += 1

        if apply:
            db.commit()
        else:
            db.rollback()
        return stats
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    stats = import_configs(Path(args.config_dir), apply=args.apply)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"{mode} import from {args.config_dir}")
    for key, value in stats.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
