"""Audit OLT running-config backups against ONT inventory."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OltConfigBackup, OLTDevice, OntUnit
from app.services.network.serial_utils import (
    normalize as normalize_serial,
)
from app.services.network.serial_utils import (
    parse_ont_id_on_olt,
)
from app.services.network.serial_utils import (
    search_candidates as serial_search_candidates,
)

_INTERFACE_GPON_RE = re.compile(
    r"^\s*interface\s+(?:gpon|xgpon|epon)\s+(\d+/\d+)\s*$", re.IGNORECASE
)
_ONT_ADD_RE = re.compile(
    r"""\bont\s+add\s+
        (?P<port>\d+)\s+
        (?P<ont_id>\d+)\s+
        sn-auth\s+"?(?P<serial>[A-Fa-f0-9A-Za-z:-]+)"?
        (?P<rest>.*)$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_SERVICE_PORT_RE = re.compile(
    r"""\bservice-port\s+
        (?P<index>\d+)\s+
        vlan\s+(?P<vlan>\d+)\s+
        (?P<pon_type>gpon|xgpon|epon)\s+(?P<fsp>\d+/\d+/\d+)\s+
        ont\s+(?P<ont_id>\d+)\s+
        gemport\s+(?P<gem>\d+)
        (?P<rest>.*)$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_LINE_PROFILE_RE = re.compile(r"ont-lineprofile-id\s+(\d+)", re.IGNORECASE)
_SRV_PROFILE_RE = re.compile(r"ont-srvprofile-id\s+(\d+)", re.IGNORECASE)
_DESC_RE = re.compile(r'\bdesc\s+"([^"]*)"', re.IGNORECASE)
_USER_VLAN_RE = re.compile(r"user-vlan\s+(\S+)", re.IGNORECASE)
_TAG_TRANSFORM_RE = re.compile(r"tag-transform\s+(\S+)", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedOntConfig:
    """One ONT registration found in an OLT running config."""

    fsp: str
    ont_id: int
    serial_number: str
    raw_serial: str
    line_profile_id: int | None = None
    service_profile_id: int | None = None
    description: str | None = None


@dataclass(frozen=True)
class ParsedServicePortConfig:
    """One service-port binding found in an OLT running config."""

    index: int
    vlan_id: int
    fsp: str
    ont_id: int
    gem_index: int
    pon_type: str
    user_vlan: str | None = None
    tag_transform: str | None = None


@dataclass(frozen=True)
class ParsedOltConfig:
    """Structured config extracted from one OLT backup."""

    ont_registrations: list[ParsedOntConfig]
    service_ports: list[ParsedServicePortConfig]


@dataclass(frozen=True)
class OntConfigCoverage:
    """Coverage result for one DB ONT."""

    ont_unit_id: uuid.UUID
    serial_number: str
    olt_device_id: uuid.UUID | None
    expected_fsp: str | None
    expected_ont_id: int | None
    found_registration: bool
    found_service_ports: int
    registration: ParsedOntConfig | None = None
    service_ports: list[ParsedServicePortConfig] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OltConfigAuditReport:
    """Audit report for one OLT backup vs DB inventory."""

    olt_device_id: uuid.UUID
    olt_name: str
    backup_id: uuid.UUID | None
    backup_created_at: datetime | None
    backup_file_path: str | None
    ont_count: int
    registered_in_config: int
    with_service_ports: int
    orphan_registrations: list[ParsedOntConfig]
    orphan_service_ports: list[ParsedServicePortConfig]
    ont_coverages: list[OntConfigCoverage]
    gaps: list[str]


def _decode_huawei_serial(raw_serial: str) -> str:
    serial = normalize_serial(raw_serial)
    if len(serial) == 16 and re.fullmatch(r"[0-9A-F]{16}", serial):
        try:
            vendor = bytes.fromhex(serial[:8]).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            return serial
        if vendor.isalpha():
            return f"{vendor.upper()}{serial[8:]}"
    return serial


def _extract_int(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text)
    if not match:
        return None
    return int(match.group(1))


def _extract_text(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def parse_huawei_running_config(config_text: str) -> ParsedOltConfig:
    """Parse ONT registrations and service-ports from Huawei running config."""
    board: str | None = None
    registrations: list[ParsedOntConfig] = []
    service_ports: list[ParsedServicePortConfig] = []

    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if not line or line == "#":
            continue

        interface_match = _INTERFACE_GPON_RE.match(line)
        if interface_match:
            board = interface_match.group(1)
            continue
        if line.lower() == "quit":
            board = None
            continue

        ont_match = _ONT_ADD_RE.search(line)
        if ont_match and board:
            port = ont_match.group("port")
            ont_id = int(ont_match.group("ont_id"))
            raw_serial = ont_match.group("serial")
            rest = ont_match.group("rest") or ""
            registrations.append(
                ParsedOntConfig(
                    fsp=f"{board}/{port}",
                    ont_id=ont_id,
                    serial_number=_decode_huawei_serial(raw_serial),
                    raw_serial=normalize_serial(raw_serial),
                    line_profile_id=_extract_int(_LINE_PROFILE_RE, rest),
                    service_profile_id=_extract_int(_SRV_PROFILE_RE, rest),
                    description=_extract_text(_DESC_RE, rest),
                )
            )
            continue

        service_match = _SERVICE_PORT_RE.search(line)
        if service_match:
            rest = service_match.group("rest") or ""
            service_ports.append(
                ParsedServicePortConfig(
                    index=int(service_match.group("index")),
                    vlan_id=int(service_match.group("vlan")),
                    fsp=service_match.group("fsp"),
                    ont_id=int(service_match.group("ont_id")),
                    gem_index=int(service_match.group("gem")),
                    pon_type=service_match.group("pon_type").lower(),
                    user_vlan=_extract_text(_USER_VLAN_RE, rest),
                    tag_transform=_extract_text(_TAG_TRANSFORM_RE, rest),
                )
            )

    return ParsedOltConfig(
        ont_registrations=registrations,
        service_ports=service_ports,
    )


def latest_valid_backup(db: Session, olt_id: uuid.UUID) -> OltConfigBackup | None:
    """Return latest non-trivial OLT backup for config audit."""
    stmt = (
        select(OltConfigBackup)
        .where(OltConfigBackup.olt_device_id == olt_id)
        .where(OltConfigBackup.file_size_bytes.is_not(None))
        .where(OltConfigBackup.file_size_bytes >= 1024)
        .order_by(OltConfigBackup.created_at.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()


def _backup_path(backup: OltConfigBackup, base_dir: Path) -> Path:
    return base_dir / backup.file_path


def _ont_fsp(ont: OntUnit) -> str | None:
    if ont.board and ont.port:
        return f"{ont.board}/{ont.port}"
    return None


def _serial_keys(value: str | None) -> set[str]:
    return {
        normalize_serial(candidate)
        for candidate in serial_search_candidates(value)
        if normalize_serial(candidate)
    }


def audit_olt_config_backup(
    db: Session,
    olt: OLTDevice,
    *,
    backup: OltConfigBackup | None = None,
    backup_base_dir: Path | None = None,
) -> OltConfigAuditReport:
    """Compare one OLT's latest backup with active ONTs assigned to that OLT."""
    backup = backup or latest_valid_backup(db, olt.id)
    assigned_onts = list(
        db.scalars(
            select(OntUnit)
            .where(OntUnit.olt_device_id == olt.id)
            .where(OntUnit.is_active.is_(True))
            .order_by(OntUnit.serial_number)
        ).all()
    )

    if backup is None:
        return OltConfigAuditReport(
            olt_device_id=olt.id,
            olt_name=olt.name,
            backup_id=None,
            backup_created_at=None,
            backup_file_path=None,
            ont_count=len(assigned_onts),
            registered_in_config=0,
            with_service_ports=0,
            orphan_registrations=[],
            orphan_service_ports=[],
            ont_coverages=[
                OntConfigCoverage(
                    ont_unit_id=ont.id,
                    serial_number=ont.serial_number,
                    olt_device_id=ont.olt_device_id,
                    expected_fsp=_ont_fsp(ont),
                    expected_ont_id=parse_ont_id_on_olt(ont.external_id),
                    found_registration=False,
                    found_service_ports=0,
                    gaps=["missing_valid_olt_backup"],
                )
                for ont in assigned_onts
            ],
            gaps=["missing_valid_olt_backup"],
        )

    base_dir = backup_base_dir or Path("/app/uploads/olt_config_backups")
    config_text = _backup_path(backup, base_dir).read_text(errors="replace")
    parsed = parse_huawei_running_config(config_text)

    regs_by_serial: dict[str, ParsedOntConfig] = {}
    for registration in parsed.ont_registrations:
        for key in _serial_keys(registration.serial_number) | _serial_keys(
            registration.raw_serial
        ):
            regs_by_serial[key] = registration

    service_ports_by_location: dict[tuple[str, int], list[ParsedServicePortConfig]] = {}
    for port in parsed.service_ports:
        service_ports_by_location.setdefault((port.fsp, port.ont_id), []).append(port)

    matched_registrations: set[tuple[str, int, str]] = set()
    matched_service_ports: set[int] = set()
    coverages: list[OntConfigCoverage] = []
    registered_count = 0
    service_port_count = 0

    for ont in assigned_onts:
        expected_fsp = _ont_fsp(ont)
        expected_ont_id = parse_ont_id_on_olt(ont.external_id)
        registration = None
        for key in _serial_keys(ont.serial_number):
            registration = regs_by_serial.get(key)
            if registration:
                break

        gaps: list[str] = []
        if registration is None:
            gaps.append("missing_olt_registration")
        else:
            registered_count += 1
            matched_registrations.add(
                (registration.fsp, registration.ont_id, registration.serial_number)
            )
            if expected_fsp and registration.fsp != expected_fsp:
                gaps.append("olt_fsp_mismatch")
            if expected_ont_id is not None and registration.ont_id != expected_ont_id:
                gaps.append("olt_ont_id_mismatch")

        location_fsp = registration.fsp if registration else expected_fsp
        location_ont_id = registration.ont_id if registration else expected_ont_id
        service_ports: list[ParsedServicePortConfig] = []
        if location_fsp and location_ont_id is not None:
            service_ports = service_ports_by_location.get(
                (location_fsp, location_ont_id), []
            )
        if service_ports:
            service_port_count += 1
            matched_service_ports.update(port.index for port in service_ports)
        else:
            gaps.append("missing_service_port")

        coverages.append(
            OntConfigCoverage(
                ont_unit_id=ont.id,
                serial_number=ont.serial_number,
                olt_device_id=ont.olt_device_id,
                expected_fsp=expected_fsp,
                expected_ont_id=expected_ont_id,
                found_registration=registration is not None,
                found_service_ports=len(service_ports),
                registration=registration,
                service_ports=service_ports,
                gaps=gaps,
            )
        )

    orphan_registrations = [
        registration
        for registration in parsed.ont_registrations
        if (registration.fsp, registration.ont_id, registration.serial_number)
        not in matched_registrations
    ]
    orphan_service_ports = [
        port for port in parsed.service_ports if port.index not in matched_service_ports
    ]

    report_gaps: list[str] = []
    if orphan_registrations:
        report_gaps.append("orphan_olt_registrations")
    if orphan_service_ports:
        report_gaps.append("orphan_service_ports")
    if any(coverage.gaps for coverage in coverages):
        report_gaps.append("ont_inventory_gaps")

    return OltConfigAuditReport(
        olt_device_id=olt.id,
        olt_name=olt.name,
        backup_id=backup.id,
        backup_created_at=backup.created_at,
        backup_file_path=backup.file_path,
        ont_count=len(assigned_onts),
        registered_in_config=registered_count,
        with_service_ports=service_port_count,
        orphan_registrations=orphan_registrations,
        orphan_service_ports=orphan_service_ports,
        ont_coverages=coverages,
        gaps=report_gaps,
    )


def audit_all_olt_config_backups(
    db: Session,
    *,
    backup_base_dir: Path | None = None,
) -> list[OltConfigAuditReport]:
    """Audit latest valid backup for every active OLT."""
    olts = list(
        db.scalars(
            select(OLTDevice)
            .where(OLTDevice.is_active.is_(True))
            .order_by(OLTDevice.name)
        ).all()
    )
    return [
        audit_olt_config_backup(db, olt, backup_base_dir=backup_base_dir)
        for olt in olts
    ]
