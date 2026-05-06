"""Import live OLT profile state into structured DB tables.

Provisioning should use imported records, not live heuristics. This service reads
the OLT once, upserts the observed profiles/registrations, and derives per-OLT
equipment-to-profile mappings from actual ONT registrations.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    OLTDevice,
    OltLineProfile,
    OltOntRegistration,
    OltOnuTypeProfileMapping,
    OltServiceProfile,
    OnuType,
    PonPort,
)
from app.services.network.huawei_command_profiles import get_huawei_command_profile
from app.services.network.olt_profile_resolution import (
    parse_line_profile_tr069_enabled,
    parse_service_profile_detail,
)
from app.services.network.olt_ssh_profiles import _parse_profile_table
from app.services.network.parsers import parse_ont_info, parse_ont_info_detail

logger = logging.getLogger(__name__)

_ONT_ADD_RE = re.compile(
    r"\bont\s+add\s+(?P<port>\d+)\s+(?P<ont_id>\d+)\s+"
    r"sn-auth\s+\"(?P<serial>[^\"]+)\".*?"
    r"ont-lineprofile-id\s+(?P<line>\d+).*?"
    r"ont-srvprofile-id\s+(?P<service>\d+)"
    r"(?:\s+desc\s+\"(?P<desc>[^\"]*)\")?",
    re.IGNORECASE | re.DOTALL,
)
_TR069_BIND_RE = re.compile(
    r"\bont\s+tr069-server-config\s+(?P<port>\d+)\s+(?P<ont_id>\d+)"
    r"\s+profile-id\s+(?P<profile_id>\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OltStateImportResult:
    success: bool
    message: str
    olt_id: str
    line_profiles: int = 0
    service_profiles: int = 0
    ont_registrations: int = 0
    profile_mappings: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "message": self.message,
            "olt_id": self.olt_id,
            "line_profiles": self.line_profiles,
            "service_profiles": self.service_profiles,
            "ont_registrations": self.ont_registrations,
            "profile_mappings": self.profile_mappings,
            "warnings": list(self.warnings),
        }


def _upsert_by_keys(
    db: Session,
    model,
    key_values: dict[str, object],
    values: dict[str, object],
):
    stmt = select(model)
    for key, value in key_values.items():
        stmt = stmt.where(getattr(model, key) == value)
    row = db.scalars(stmt).first()
    if row is None:
        row = model(**key_values)
        db.add(row)
    for key, value in values.items():
        setattr(row, key, value)
    return row


def _onu_type_id_by_equipment(db: Session) -> dict[str, object]:
    rows = db.scalars(select(OnuType).where(OnuType.is_active.is_(True))).all()
    return {
        str(row.name or "").strip().upper(): row.id
        for row in rows
        if str(row.name or "").strip()
    }


def _looks_like_equipment_profile(name: str | None) -> bool:
    clean = str(name or "").strip()
    if not clean:
        return False
    lowered = clean.lower()
    if lowered.startswith(("onu-type-", "srv-profile_", "line-profile_", "spl_")):
        return False
    return bool(re.search(r"\d", clean))


def _plain_serial_or_none(value: str | None) -> str | None:
    clean = str(value or "").strip().upper()
    if re.fullmatch(r"[0-9A-F]{12,32}", clean):
        return clean
    return None


def _import_profile_mappings(
    db: Session,
    olt: OLTDevice,
    imported_at: datetime,
    warnings: list[str],
) -> int:
    registrations = db.scalars(
        select(OltOntRegistration)
        .where(OltOntRegistration.olt_id == olt.id)
        .where(OltOntRegistration.is_active.is_(True))
        .where(OltOntRegistration.equipment_id.isnot(None))
        .where(OltOntRegistration.line_profile_id.isnot(None))
        .where(OltOntRegistration.service_profile_id.isnot(None))
    ).all()
    counts: dict[str, Counter[tuple[int, int]]] = {}
    for registration in registrations:
        equipment_id = str(registration.equipment_id or "").strip()
        if not equipment_id:
            continue
        pair = (int(registration.line_profile_id), int(registration.service_profile_id))
        counts.setdefault(equipment_id, Counter())[pair] += 1

    onu_types = _onu_type_id_by_equipment(db)
    imported = 0
    for equipment_id, profile_counts in counts.items():
        if not profile_counts:
            continue
        if len(profile_counts) > 1:
            profile_pairs = ", ".join(
                f"line {line}/service {service} ({count})"
                for (line, service), count in sorted(profile_counts.items())
            )
            warnings.append(
                f"Ambiguous imported profile mapping for {equipment_id}: {profile_pairs}."
            )
            continue
        (line_profile_id, service_profile_id), count = next(
            iter(profile_counts.items())
        )
        _upsert_by_keys(
            db,
            OltOnuTypeProfileMapping,
            {"olt_id": olt.id, "equipment_id": equipment_id},
            {
                "onu_type_id": onu_types.get(equipment_id.upper()),
                "line_profile_id": line_profile_id,
                "service_profile_id": service_profile_id,
                "source_registration_count": int(count),
                "last_imported_at": imported_at,
            },
        )
        imported += 1
    return imported


def _import_named_service_profile_mappings(
    db: Session,
    olt: OLTDevice,
    imported_at: datetime,
    warnings: list[str],
) -> int:
    registrations = db.scalars(
        select(OltOntRegistration)
        .where(OltOntRegistration.olt_id == olt.id)
        .where(OltOntRegistration.is_active.is_(True))
        .where(OltOntRegistration.line_profile_id.isnot(None))
        .where(OltOntRegistration.service_profile_id.isnot(None))
    ).all()
    service_profiles = {
        row.profile_id: row
        for row in db.scalars(
            select(OltServiceProfile).where(OltServiceProfile.olt_id == olt.id)
        ).all()
    }
    onu_types = _onu_type_id_by_equipment(db)
    counts: dict[str, Counter[int]] = {}
    service_ids: dict[str, int] = {}
    for registration in registrations:
        service_profile = service_profiles.get(int(registration.service_profile_id))
        if service_profile is None or not _looks_like_equipment_profile(
            service_profile.name
        ):
            continue
        equipment_id = str(service_profile.name or "").strip()
        service_ids[equipment_id] = int(service_profile.profile_id)
        counts.setdefault(equipment_id, Counter())[int(registration.line_profile_id)] += 1

    imported = 0
    for equipment_id, line_counts in counts.items():
        if len(line_counts) > 1:
            profile_pairs = ", ".join(
                f"line {line}/service {service_ids[equipment_id]} ({count})"
                for line, count in sorted(line_counts.items())
            )
            warnings.append(
                f"Ambiguous imported profile mapping for {equipment_id}: {profile_pairs}."
            )
            continue
        line_profile_id, count = next(iter(line_counts.items()))
        _upsert_by_keys(
            db,
            OltOnuTypeProfileMapping,
            {"olt_id": olt.id, "equipment_id": equipment_id},
            {
                "onu_type_id": onu_types.get(equipment_id.upper()),
                "line_profile_id": line_profile_id,
                "service_profile_id": service_ids[equipment_id],
                "source_registration_count": int(count),
                "last_imported_at": imported_at,
            },
        )
        imported += 1
    return imported


def _read_dump_file(dump_dir: Path, filename: str) -> str:
    path = dump_dir / filename
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _board_from_running_config(config_text: str) -> str:
    match = re.search(r"^\s*interface\s+gpon\s+(\d+/\d+)\s*$", config_text, re.M)
    return match.group(1) if match else "0/1"


def _iter_gpon_config_blocks(config_text: str) -> list[tuple[str, str]]:
    blocks = [
        (match.group(1), match.group("body"))
        for match in re.finditer(
            r"^\s*interface\s+gpon\s+(\d+/\d+)\s*$"
            r"(?P<body>.*?)(?=^\s*interface\s+\S+|\Z)",
            config_text,
            re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
    ]
    if blocks:
        return blocks
    return [(_board_from_running_config(config_text), config_text)]


def _parse_tr069_bindings(config_text: str) -> dict[tuple[str, int, int], int]:
    bindings: dict[tuple[str, int, int], int] = {}
    for board, body in _iter_gpon_config_blocks(config_text):
        for match in _TR069_BIND_RE.finditer(body):
            bindings[
                (board, int(match.group("port")), int(match.group("ont_id")))
            ] = int(match.group("profile_id"))
    return bindings


def import_olt_state_from_dump(
    db: Session,
    olt_id: str,
    dump_dir: str | Path,
) -> OltStateImportResult:
    """Import OLT state from an audit dump directory."""
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return OltStateImportResult(
            success=False,
            message="OLT not found",
            olt_id=str(olt_id),
        )

    dump_path = Path(dump_dir)
    olt_name = olt.name
    imported_at = datetime.now(UTC)
    warnings: list[str] = []

    line_output = _read_dump_file(dump_path, "10_ont_lineprofile_all.txt")
    service_output = _read_dump_file(dump_path, "11_ont_srvprofile_all.txt")
    running_config = _read_dump_file(dump_path, "99_running_config.txt")
    if not line_output or not service_output or not running_config:
        return OltStateImportResult(
            success=False,
            message="Dump is missing line profiles, service profiles, or running config.",
            olt_id=str(olt.id),
        )

    try:
        line_profiles = _parse_profile_table(line_output)
        service_profiles = _parse_profile_table(service_output)
        if not line_profiles:
            return OltStateImportResult(
                success=False,
                message="Dump has no line profiles.",
                olt_id=str(olt.id),
            )
        if not service_profiles:
            return OltStateImportResult(
                success=False,
                message="Dump has no service profiles.",
                olt_id=str(olt.id),
            )

        for profile in line_profiles:
            _upsert_by_keys(
                db,
                OltLineProfile,
                {"olt_id": olt.id, "profile_id": profile.profile_id},
                {
                    "name": profile.name,
                    "binding_count": profile.binding_count,
                    "tr069_management_enabled": None,
                    "raw_config": line_output,
                    "last_imported_at": imported_at,
                },
            )

        for profile in service_profiles:
            _upsert_by_keys(
                db,
                OltServiceProfile,
                {"olt_id": olt.id, "profile_id": profile.profile_id},
                {
                    "name": profile.name,
                    "binding_count": profile.binding_count,
                    "ethernet_ports": None,
                    "voip_ports": None,
                    "catv_ports": None,
                    "raw_config": service_output,
                    "last_imported_at": imported_at,
                },
            )

        db.flush()
        service_profiles_by_id = {
            int(profile.profile_id): profile for profile in service_profiles
        }
        named_profile_counts: dict[str, Counter[int]] = {}
        named_profile_service_ids: dict[str, int] = {}
        tr069_bindings = _parse_tr069_bindings(running_config)
        seen_registration_keys: set[tuple[str, int]] = set()
        registration_count = 0
        for board, body in _iter_gpon_config_blocks(running_config):
            for match in _ONT_ADD_RE.finditer(body):
                port = int(match.group("port"))
                ont_id = int(match.group("ont_id"))
                fsp = f"{board}/{port}"
                serial_number = _plain_serial_or_none(match.group("serial"))
                line_profile_id = int(match.group("line"))
                service_profile_id = int(match.group("service"))
                registration_key = (fsp, ont_id)
                seen_registration_keys.add(registration_key)
                service_profile = service_profiles_by_id.get(service_profile_id)
                if service_profile is not None and _looks_like_equipment_profile(
                    service_profile.name
                ):
                    equipment_id = str(service_profile.name or "").strip()
                    named_profile_service_ids[equipment_id] = service_profile_id
                    named_profile_counts.setdefault(equipment_id, Counter())[
                        line_profile_id
                    ] += 1
                if serial_number:
                    for moved_row in db.scalars(
                        select(OltOntRegistration)
                        .where(OltOntRegistration.olt_id == olt.id)
                        .where(OltOntRegistration.serial_number == serial_number)
                        .where(OltOntRegistration.is_active.is_(True))
                    ).all():
                        if (moved_row.fsp, moved_row.ont_id_on_olt) != registration_key:
                            moved_row.is_active = False
                            moved_row.last_imported_at = imported_at

                _upsert_by_keys(
                    db,
                    OltOntRegistration,
                    {
                        "olt_id": olt.id,
                        "fsp": fsp,
                        "ont_id_on_olt": ont_id,
                    },
                    {
                        "serial_number": serial_number,
                        "equipment_id": None,
                        "line_profile_id": line_profile_id,
                        "service_profile_id": service_profile_id,
                        "tr069_profile_id": tr069_bindings.get((board, port, ont_id)),
                        "match_state": None,
                        "description": (match.group("desc") or "").strip() or None,
                        "raw_config": match.group(0),
                        "is_active": True,
                        "last_imported_at": imported_at,
                    },
                )
                registration_count += 1

        if seen_registration_keys:
            for row in db.scalars(
                select(OltOntRegistration)
                .where(OltOntRegistration.olt_id == olt.id)
                .where(OltOntRegistration.is_active.is_(True))
            ).all():
                if (row.fsp, row.ont_id_on_olt) not in seen_registration_keys:
                    row.is_active = False
                    row.last_imported_at = imported_at

        onu_types = _onu_type_id_by_equipment(db)
        mapping_count = 0
        for equipment_id, line_counts in named_profile_counts.items():
            if len(line_counts) > 1:
                profile_pairs = ", ".join(
                    f"line {line}/service {named_profile_service_ids[equipment_id]} ({count})"
                    for line, count in sorted(line_counts.items())
                )
                warnings.append(
                    f"Ambiguous imported profile mapping for {equipment_id}: {profile_pairs}."
                )
                continue
            line_profile_id, count = next(iter(line_counts.items()))
            _upsert_by_keys(
                db,
                OltOnuTypeProfileMapping,
                {"olt_id": olt.id, "equipment_id": equipment_id},
                {
                    "onu_type_id": onu_types.get(equipment_id.upper()),
                    "line_profile_id": line_profile_id,
                    "service_profile_id": named_profile_service_ids[equipment_id],
                    "source_registration_count": int(count),
                    "last_imported_at": imported_at,
                },
            )
            mapping_count += 1
        db.flush()
        return OltStateImportResult(
            success=True,
            message="OLT state imported from dump.",
            olt_id=str(olt.id),
            line_profiles=len(line_profiles),
            service_profiles=len(service_profiles),
            ont_registrations=registration_count,
            profile_mappings=mapping_count,
            warnings=warnings,
        )
    except Exception as exc:
        logger.exception("OLT dump import failed for %s from %s", olt_name, dump_path)
        db.rollback()
        return OltStateImportResult(
            success=False,
            message=f"OLT dump import failed: {exc}",
            olt_id=str(olt.id),
            warnings=warnings,
        )


def import_olt_state(db: Session, olt_id: str) -> OltStateImportResult:
    """Import OLT profile/registration state into DB records.

    This function is intentionally not a validator. It stores the observed OLT
    state and derives profile mappings from actual imported ONT registrations.
    """
    from app.services.network import olt_ssh as core

    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return OltStateImportResult(
            success=False,
            message="OLT not found",
            olt_id=str(olt_id),
        )

    imported_at = datetime.now(UTC)
    olt_name = olt.name
    warnings: list[str] = []
    line_count = 0
    service_count = 0
    registration_count = 0

    try:
        transport, channel, _policy = core._open_shell(olt)
    except Exception as exc:
        return OltStateImportResult(
            success=False,
            message=f"Connection failed: {exc}",
            olt_id=str(olt.id),
        )

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        core._run_huawei_cmd(channel, "screen-length 0 temporary")

        line_output = core._run_huawei_cmd(channel, "display ont-lineprofile gpon all")
        line_profiles = _parse_profile_table(line_output)
        if not line_profiles:
            return OltStateImportResult(
                success=False,
                message="OLT has no imported line profiles.",
                olt_id=str(olt.id),
            )
        for profile in line_profiles:
            detail = core._run_huawei_cmd(
                channel, f"display ont-lineprofile gpon profile-id {profile.profile_id}"
            )
            _upsert_by_keys(
                db,
                OltLineProfile,
                {"olt_id": olt.id, "profile_id": profile.profile_id},
                {
                    "name": profile.name,
                    "binding_count": profile.binding_count,
                    "tr069_management_enabled": parse_line_profile_tr069_enabled(detail),
                    "raw_config": detail,
                    "last_imported_at": imported_at,
                },
            )
            line_count += 1

        service_output = core._run_huawei_cmd(
            channel, "display ont-srvprofile gpon all"
        )
        service_profiles = _parse_profile_table(service_output)
        if not service_profiles:
            return OltStateImportResult(
                success=False,
                message="OLT has no imported service profiles.",
                olt_id=str(olt.id),
            )
        for profile in service_profiles:
            detail = core._run_huawei_cmd(
                channel, f"display ont-srvprofile gpon profile-id {profile.profile_id}"
            )
            parsed = parse_service_profile_detail(
                detail,
                profile_id=profile.profile_id,
                name=profile.name,
                binding_count=profile.binding_count,
            )
            _upsert_by_keys(
                db,
                OltServiceProfile,
                {"olt_id": olt.id, "profile_id": profile.profile_id},
                {
                    "name": profile.name,
                    "binding_count": profile.binding_count,
                    "ethernet_ports": parsed.ethernet_ports,
                    "voip_ports": parsed.voip_ports,
                    "catv_ports": parsed.catv_ports,
                    "raw_config": detail,
                    "last_imported_at": imported_at,
                },
            )
            service_count += 1

        db.flush()
        command_profile = get_huawei_command_profile(olt)
        pon_ports = db.scalars(
            select(PonPort)
            .where(PonPort.olt_id == olt.id)
            .where(PonPort.is_active.is_(True))
            .order_by(PonPort.name)
        ).all()
        if not pon_ports:
            warnings.append("No active PON ports in DB; ONT registrations not imported.")

        seen_registration_keys: set[tuple[str, int]] = set()
        for pon_port in pon_ports:
            fsp = str(pon_port.name or "").strip()
            if not fsp:
                continue
            try:
                summary_output = core._run_huawei_cmd(
                    channel,
                    command_profile.display_ont_info_all(fsp),
                )
            except ValueError as exc:
                warnings.append(f"Skipped {fsp}: {exc}")
                continue
            summary = parse_ont_info(summary_output)
            for entry in summary.data:
                detail_fsp = entry.fsp or fsp
                try:
                    detail_cmd = command_profile.display_ont_info(
                        detail_fsp,
                        entry.ont_id,
                    )
                    detail_output = core._run_huawei_cmd(channel, detail_cmd)
                except ValueError as exc:
                    warnings.append(f"Skipped ONT detail {detail_fsp} {entry.ont_id}: {exc}")
                    continue
                detail = parse_ont_info_detail(detail_output)
                if detail is None:
                    continue
                registration_fsp = detail.fsp or detail_fsp
                registration_ont_id = detail.ont_id or entry.ont_id
                seen_registration_keys.add((registration_fsp, registration_ont_id))
                serial_number = detail.serial_number or entry.serial_number
                if serial_number:
                    moved_rows = db.scalars(
                        select(OltOntRegistration)
                        .where(OltOntRegistration.olt_id == olt.id)
                        .where(OltOntRegistration.serial_number == serial_number)
                        .where(OltOntRegistration.is_active.is_(True))
                    ).all()
                    for moved_row in moved_rows:
                        if (
                            moved_row.fsp,
                            moved_row.ont_id_on_olt,
                        ) != (registration_fsp, registration_ont_id):
                            moved_row.is_active = False
                            moved_row.last_imported_at = imported_at
                _upsert_by_keys(
                    db,
                    OltOntRegistration,
                    {
                        "olt_id": olt.id,
                        "fsp": registration_fsp,
                        "ont_id_on_olt": registration_ont_id,
                    },
                    {
                        "serial_number": serial_number,
                        "equipment_id": detail.equipment_id or detail.model,
                        "line_profile_id": detail.line_profile_id,
                        "service_profile_id": detail.service_profile_id,
                        "tr069_profile_id": detail.tr069_profile_id,
                        "match_state": detail.match_state or entry.match_state,
                        "description": detail.description or entry.description,
                        "raw_config": detail_output,
                        "is_active": True,
                        "last_imported_at": imported_at,
                    },
                )
                registration_count += 1

        if seen_registration_keys:
            existing = db.scalars(
                select(OltOntRegistration)
                .where(OltOntRegistration.olt_id == olt.id)
                .where(OltOntRegistration.is_active.is_(True))
            ).all()
            for row in existing:
                if (row.fsp, row.ont_id_on_olt) not in seen_registration_keys:
                    row.is_active = False
                    row.last_imported_at = imported_at

        mapping_count = _import_profile_mappings(db, olt, imported_at, warnings)
        db.flush()
        return OltStateImportResult(
            success=True,
            message="OLT state imported.",
            olt_id=str(olt.id),
            line_profiles=line_count,
            service_profiles=service_count,
            ont_registrations=registration_count,
            profile_mappings=mapping_count,
            warnings=warnings,
        )
    except Exception as exc:
        logger.exception("OLT state import failed for %s", olt_name)
        db.rollback()
        return OltStateImportResult(
            success=False,
            message=f"OLT state import failed: {exc}",
            olt_id=str(olt.id),
            warnings=warnings,
        )
    finally:
        transport.close()
