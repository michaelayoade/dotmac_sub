#!/usr/bin/env python3
"""Verify ONT setup by comparing OLT backups against database records."""

import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, func
from app.db import SessionLocal
from app.models.network import (
    OLTDevice,
    OntUnit,
    OntAssignment,
    OltConfigBackup,
    OltOntRegistration,
    OltServicePort,
)


@dataclass
class ParsedOnt:
    fsp: str
    ont_id: int
    serial: str
    line_profile_id: int | None = None
    service_profile_id: int | None = None
    description: str | None = None


@dataclass
class ParsedServicePort:
    index: int
    vlan: int
    fsp: str
    ont_id: int
    gem_index: int


@dataclass
class OltVerificationReport:
    olt_id: str
    olt_name: str
    backup_file: str | None = None
    backup_date: str | None = None
    # Counts
    db_ont_count: int = 0
    config_ont_count: int = 0
    matched_count: int = 0
    # Issues
    missing_from_config: list = field(default_factory=list)  # In DB but not config
    missing_from_db: list = field(default_factory=list)      # In config but not DB
    fsp_mismatches: list = field(default_factory=list)       # FSP doesn't match
    ont_id_mismatches: list = field(default_factory=list)    # ONT-ID doesn't match
    missing_service_ports: list = field(default_factory=list)
    errors: list = field(default_factory=list)


# Regex patterns for Huawei OLT config
ONT_ADD_PATTERN = re.compile(
    r"ont\s+add\s+(?P<port>\d+)\s+(?P<ont_id>\d+)\s+"
    r"(?:sn-auth\s+)?[\"']?(?P<serial>[A-Fa-f0-9A-Za-z:-]+)[\"']?"
    r"(?:\s+omci\s+ont-lineprofile-id\s+(?P<line_profile>\d+))?"
    r"(?:\s+ont-srvprofile-id\s+(?P<service_profile>\d+))?"
    r'(?:\s+desc\s+"(?P<desc>[^"]*)")?',
    re.IGNORECASE
)

SERVICE_PORT_PATTERN = re.compile(
    r"service-port\s+(?P<index>\d+)\s+vlan\s+(?P<vlan>\d+)\s+"
    r"(?:gpon|xgpon|epon)\s+(?P<fsp>\d+/\d+/\d+)\s+"
    r"ont\s+(?P<ont_id>\d+)\s+gemport\s+(?P<gem>\d+)",
    re.IGNORECASE
)


def normalize_serial(serial: str) -> str:
    """Normalize ONT serial number for comparison."""
    serial = serial.upper().strip()
    # Remove common prefixes/separators
    serial = serial.replace("-", "").replace(":", "").replace(" ", "")
    # Handle Huawei hex encoding
    if len(serial) == 16 and all(c in "0123456789ABCDEF" for c in serial):
        # Try to decode as ASCII hex
        try:
            prefix = bytes.fromhex(serial[:8]).decode("ascii", errors="ignore")
            if prefix.isalnum():
                serial = prefix + serial[8:]
        except Exception:
            pass
    return serial


def parse_config_file(filepath: str) -> tuple[dict[str, ParsedOnt], dict[str, list[ParsedServicePort]]]:
    """Parse ONT registrations and service ports from config file."""
    onts: dict[str, ParsedOnt] = {}  # serial -> ParsedOnt
    service_ports: dict[str, list[ParsedServicePort]] = defaultdict(list)  # fsp:ont_id -> ports

    current_interface = None

    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        return {}, {}

    # Find interface sections
    interface_pattern = re.compile(r"interface\s+(?:gpon|epon|xgpon)\s+(\d+/\d+)", re.IGNORECASE)

    lines = content.split("\n")
    for i, line in enumerate(lines):
        # Track current interface
        iface_match = interface_pattern.search(line)
        if iface_match:
            current_interface = iface_match.group(1)
            continue

        # Parse ONT add commands
        ont_match = ONT_ADD_PATTERN.search(line)
        if ont_match and current_interface:
            port = ont_match.group("port")
            ont_id = int(ont_match.group("ont_id"))
            serial = normalize_serial(ont_match.group("serial"))
            fsp = f"{current_interface}/{port}"

            onts[serial] = ParsedOnt(
                fsp=fsp,
                ont_id=ont_id,
                serial=serial,
                line_profile_id=int(ont_match.group("line_profile")) if ont_match.group("line_profile") else None,
                service_profile_id=int(ont_match.group("service_profile")) if ont_match.group("service_profile") else None,
                description=ont_match.group("desc"),
            )

        # Parse service-port commands
        sp_match = SERVICE_PORT_PATTERN.search(line)
        if sp_match:
            fsp = sp_match.group("fsp")
            ont_id = int(sp_match.group("ont_id"))
            key = f"{fsp}:{ont_id}"
            service_ports[key].append(ParsedServicePort(
                index=int(sp_match.group("index")),
                vlan=int(sp_match.group("vlan")),
                fsp=fsp,
                ont_id=ont_id,
                gem_index=int(sp_match.group("gem")),
            ))

    return onts, service_ports


def get_latest_backup(db, olt_id: str) -> tuple[str | None, str | None]:
    """Get the latest backup file for an OLT - prefer most recent file on disk."""
    base_dir = os.getenv("OLT_BACKUP_DIR", "uploads/olt_config_backups")
    olt_dir = os.path.join(base_dir, str(olt_id))

    # First try to find the most recent file on disk
    if os.path.isdir(olt_dir):
        files = []
        for f in os.listdir(olt_dir):
            if f.endswith(".txt") or f.endswith(".cfg"):
                filepath = os.path.join(olt_dir, f)
                files.append((os.path.getmtime(filepath), filepath))
        if files:
            files.sort(reverse=True)
            filepath = files[0][1]
            mtime = os.path.getmtime(filepath)
            from datetime import datetime
            date_str = datetime.fromtimestamp(mtime).isoformat()
            return filepath, date_str

    # Fall back to database record
    backup = db.scalars(
        select(OltConfigBackup)
        .where(OltConfigBackup.olt_device_id == olt_id)
        .order_by(OltConfigBackup.created_at.desc())
        .limit(1)
    ).first()

    if not backup:
        return None, None

    filepath = os.path.join(base_dir, backup.file_path)

    if os.path.exists(filepath):
        return filepath, str(backup.created_at)

    return None, None


def verify_olt(db, olt: OLTDevice) -> OltVerificationReport:
    """Verify ONT setup for a single OLT."""
    report = OltVerificationReport(
        olt_id=str(olt.id),
        olt_name=olt.name,
    )

    # Get latest backup
    backup_path, backup_date = get_latest_backup(db, olt.id)
    if not backup_path:
        report.errors.append("No valid backup file found")
        return report

    report.backup_file = backup_path
    report.backup_date = backup_date

    # Parse config
    config_onts, config_service_ports = parse_config_file(backup_path)
    report.config_ont_count = len(config_onts)

    # Get DB ONTs for this OLT
    db_onts = list(db.scalars(
        select(OntUnit)
        .where(OntUnit.olt_device_id == olt.id)
        .where(OntUnit.is_active.is_(True))
    ).all())
    report.db_ont_count = len(db_onts)

    # Build lookup by serial
    db_ont_by_serial: dict[str, OntUnit] = {}
    for ont in db_onts:
        serial = normalize_serial(ont.serial_number or "")
        if serial:
            db_ont_by_serial[serial] = ont

    # Compare
    matched_serials = set()

    for serial, config_ont in config_onts.items():
        if serial in db_ont_by_serial:
            matched_serials.add(serial)
            db_ont = db_ont_by_serial[serial]

            # Check FSP match
            db_fsp = None
            assignment = db.scalars(
                select(OntAssignment)
                .where(OntAssignment.ont_unit_id == db_ont.id)
                .where(OntAssignment.active.is_(True))
                .limit(1)
            ).first()

            if assignment and assignment.pon_port:
                port = assignment.pon_port
                # Use port name which contains FSP like "0/1/0"
                db_fsp = port.name
            elif hasattr(db_ont, 'fsp') and db_ont.fsp:
                db_fsp = db_ont.fsp

            if db_fsp and db_fsp != config_ont.fsp:
                report.fsp_mismatches.append({
                    "serial": serial,
                    "db_fsp": db_fsp,
                    "config_fsp": config_ont.fsp,
                })

            # Check ONT-ID match
            db_ont_id = getattr(db_ont, 'olt_ont_id', None) or getattr(db_ont, 'ont_id_on_olt', None)
            if db_ont_id and db_ont_id != config_ont.ont_id:
                report.ont_id_mismatches.append({
                    "serial": serial,
                    "db_ont_id": db_ont_id,
                    "config_ont_id": config_ont.ont_id,
                })

            # Check service port exists
            sp_key = f"{config_ont.fsp}:{config_ont.ont_id}"
            if sp_key not in config_service_ports:
                report.missing_service_ports.append({
                    "serial": serial,
                    "fsp": config_ont.fsp,
                    "ont_id": config_ont.ont_id,
                })
        else:
            # In config but not in DB
            report.missing_from_db.append({
                "serial": serial,
                "fsp": config_ont.fsp,
                "ont_id": config_ont.ont_id,
                "description": config_ont.description,
            })

    # Find ONTs in DB but not in config
    for serial, db_ont in db_ont_by_serial.items():
        if serial not in matched_serials and serial not in config_onts:
            report.missing_from_config.append({
                "serial": serial,
                "ont_id": str(db_ont.id)[:8],
                "status": str(getattr(db_ont, 'authorization_status', 'unknown')),
            })

    report.matched_count = len(matched_serials)

    return report


def main():
    print("=" * 80)
    print("ONT SETUP VERIFICATION REPORT")
    print("Comparing OLT running configs against database records")
    print("=" * 80)

    db = SessionLocal()
    try:
        # Get all active OLTs
        olts = list(db.scalars(
            select(OLTDevice)
            .where(OLTDevice.status.in_(["active", "maintenance"]))
            .order_by(OLTDevice.name)
        ).all())

        print(f"\nFound {len(olts)} active OLTs to verify\n")

        total_db = 0
        total_config = 0
        total_matched = 0
        total_issues = 0

        for olt in olts:
            report = verify_olt(db, olt)

            total_db += report.db_ont_count
            total_config += report.config_ont_count
            total_matched += report.matched_count

            issues = (
                len(report.missing_from_config) +
                len(report.missing_from_db) +
                len(report.fsp_mismatches) +
                len(report.ont_id_mismatches) +
                len(report.missing_service_ports) +
                len(report.errors)
            )
            total_issues += issues

            # Print OLT summary
            status = "OK" if issues == 0 else f"ISSUES ({issues})"
            print(f"\n{'=' * 60}")
            print(f"OLT: {report.olt_name}")
            print(f"{'=' * 60}")
            print(f"  Backup: {report.backup_file or 'NONE'}")
            print(f"  Backup Date: {report.backup_date or 'N/A'}")
            print(f"  DB ONTs: {report.db_ont_count}")
            print(f"  Config ONTs: {report.config_ont_count}")
            print(f"  Matched: {report.matched_count}")
            print(f"  Status: {status}")

            if report.errors:
                print(f"\n  ERRORS:")
                for err in report.errors:
                    print(f"    - {err}")

            if report.missing_from_config:
                print(f"\n  Missing from config ({len(report.missing_from_config)}):")
                for item in report.missing_from_config[:5]:
                    print(f"    - {item['serial']} (status: {item['status']})")
                if len(report.missing_from_config) > 5:
                    print(f"    ... and {len(report.missing_from_config) - 5} more")

            if report.missing_from_db:
                print(f"\n  Missing from DB ({len(report.missing_from_db)}):")
                for item in report.missing_from_db[:5]:
                    desc = f" ({item['description']})" if item.get('description') else ""
                    print(f"    - {item['serial']} at {item['fsp']}:{item['ont_id']}{desc}")
                if len(report.missing_from_db) > 5:
                    print(f"    ... and {len(report.missing_from_db) - 5} more")

            if report.fsp_mismatches:
                print(f"\n  FSP Mismatches ({len(report.fsp_mismatches)}):")
                for item in report.fsp_mismatches[:5]:
                    print(f"    - {item['serial']}: DB={item['db_fsp']} vs Config={item['config_fsp']}")
                if len(report.fsp_mismatches) > 5:
                    print(f"    ... and {len(report.fsp_mismatches) - 5} more")

            if report.ont_id_mismatches:
                print(f"\n  ONT-ID Mismatches ({len(report.ont_id_mismatches)}):")
                for item in report.ont_id_mismatches[:5]:
                    print(f"    - {item['serial']}: DB={item['db_ont_id']} vs Config={item['config_ont_id']}")
                if len(report.ont_id_mismatches) > 5:
                    print(f"    ... and {len(report.ont_id_mismatches) - 5} more")

            if report.missing_service_ports:
                print(f"\n  Missing Service Ports ({len(report.missing_service_ports)}):")
                for item in report.missing_service_ports[:5]:
                    print(f"    - {item['serial']} at {item['fsp']}:{item['ont_id']}")
                if len(report.missing_service_ports) > 5:
                    print(f"    ... and {len(report.missing_service_ports) - 5} more")

        # Overall summary
        print("\n" + "=" * 80)
        print("OVERALL SUMMARY")
        print("=" * 80)
        print(f"  Total OLTs checked: {len(olts)}")
        print(f"  Total DB ONTs: {total_db}")
        print(f"  Total Config ONTs: {total_config}")
        print(f"  Total Matched: {total_matched}")
        print(f"  Total Issues: {total_issues}")

        match_rate = (total_matched / total_db * 100) if total_db > 0 else 0
        print(f"  Match Rate: {match_rate:.1f}%")

        if total_issues == 0:
            print("\n  All ONTs properly configured!")
        else:
            print(f"\n  WARNING: {total_issues} issues found - review above for details")

    finally:
        db.close()


if __name__ == "__main__":
    main()
