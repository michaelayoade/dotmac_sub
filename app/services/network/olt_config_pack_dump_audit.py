"""Audit OLT config packs against local Huawei running-config dumps."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice
from app.services.network.olt_config_pack import resolve_olt_config_pack

DEFAULT_DUMP_ROOTS = (
    Path("/root/dotmac-olt-configs/2026-04-17"),
    Path("/opt/dotmac_sub/uploads/olt_config_backups"),
)

_LINE_PROFILE_START_RE = re.compile(
    r'\bont-lineprofile\s+gpon\s+profile-id\s+(\d+)\s+profile-name\s+"([^"]*)"',
    re.IGNORECASE,
)
_TR069_PROFILE_RE = re.compile(
    r'\bont\s+tr069-server-profile\s+add\s+profile-id\s+(\d+)\s+profile-name\s+"([^"]*)"\s+url\s+"([^"]*)"',
    re.IGNORECASE,
)
_GEM_ADD_RE = re.compile(r"^\s*gem\s+add\s+(\d+)\b", re.IGNORECASE)
_GEM_MAPPING_RE = re.compile(r"^\s*gem\s+mapping\s+(\d+)\b", re.IGNORECASE)
_TR069_IP_INDEX_RE = re.compile(r"\btr069-ip-index\s+(\d+)\b", re.IGNORECASE)
_ONT_ADD_LINE_PROFILE_RE = re.compile(
    r"\bont\s+add\b.*?\bont-lineprofile-id\s+(\d+)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DumpLineProfile:
    profile_id: int
    name: str
    gem_indexes: set[int] = field(default_factory=set)
    tr069_management_enabled: bool = False
    tr069_ip_index: int | None = None


@dataclass(frozen=True)
class DumpTr069Profile:
    profile_id: int
    name: str
    acs_url: str


@dataclass(frozen=True)
class ParsedOltDumpProfiles:
    line_profiles: dict[int, DumpLineProfile]
    tr069_profiles: dict[int, DumpTr069Profile]
    ont_line_profile_counts: Counter[int]


@dataclass
class OltConfigPackDumpAudit:
    olt_id: str
    olt_name: str
    dump_path: str | None
    success: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    observed: dict[str, Any] = field(default_factory=dict)
    suggested_updates: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return self.success and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "olt_id": self.olt_id,
            "olt_name": self.olt_name,
            "dump_path": self.dump_path,
            "success": self.success,
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "observed": self.observed,
            "suggested_updates": self.suggested_updates,
        }


def _olt_dump_slug(olt: OLTDevice) -> str:
    return (olt.name or str(olt.id)).split()[0].lower().replace("_", "-")


def find_local_olt_dump(
    olt: OLTDevice,
    dump_roots: tuple[Path, ...] = DEFAULT_DUMP_ROOTS,
) -> Path | None:
    """Find the best local running-config dump for an OLT."""
    slug = _olt_dump_slug(olt)
    direct_candidates: list[Path] = []
    for root in dump_roots:
        direct_candidates.extend(
            candidate
            for candidate in (root / f"{slug}.cfg", root / f"{slug}.txt")
            if candidate.exists()
        )
    if direct_candidates:
        return max(direct_candidates, key=lambda path: path.stat().st_mtime)

    upload_root = next(
        (root for root in dump_roots if root.name == "olt_config_backups" and root.exists()),
        None,
    )
    if upload_root is None:
        return None
    olt_dir = upload_root / str(olt.id)
    if not olt_dir.exists():
        return None
    candidates = [
        path
        for path in olt_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".txt", ".cfg", ".log"}
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def parse_olt_dump_profiles(config_text: str) -> ParsedOltDumpProfiles:
    """Parse profile definitions and ONT profile usage from a Huawei config dump."""
    line_profiles: dict[int, DumpLineProfile] = {}
    tr069_profiles: dict[int, DumpTr069Profile] = {}
    ont_line_profile_counts: Counter[int] = Counter()

    current_id: int | None = None
    current_name = ""
    current_gems: set[int] = set()
    current_tr069 = False
    current_tr069_ip_index: int | None = None

    def flush_current() -> None:
        nonlocal current_id, current_name, current_gems, current_tr069, current_tr069_ip_index
        if current_id is None:
            return
        line_profiles[current_id] = DumpLineProfile(
            profile_id=current_id,
            name=current_name,
            gem_indexes=set(current_gems),
            tr069_management_enabled=current_tr069,
            tr069_ip_index=current_tr069_ip_index,
        )
        current_id = None
        current_name = ""
        current_gems = set()
        current_tr069 = False
        current_tr069_ip_index = None

    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        tr069_match = _TR069_PROFILE_RE.search(line)
        if tr069_match:
            profile_id = int(tr069_match.group(1))
            tr069_profiles[profile_id] = DumpTr069Profile(
                profile_id=profile_id,
                name=tr069_match.group(2),
                acs_url=tr069_match.group(3).strip(),
            )

        usage_match = _ONT_ADD_LINE_PROFILE_RE.search(line)
        if usage_match:
            ont_line_profile_counts[int(usage_match.group(1))] += 1

        start_match = _LINE_PROFILE_START_RE.search(line)
        if start_match:
            flush_current()
            current_id = int(start_match.group(1))
            current_name = start_match.group(2)
            continue

        if current_id is None:
            continue
        if line == "quit" or line.startswith("#"):
            flush_current()
            continue

        gem_match = _GEM_ADD_RE.match(line) or _GEM_MAPPING_RE.match(line)
        if gem_match:
            current_gems.add(int(gem_match.group(1)))
            continue
        if re.search(r"\btr069-management\s+enable\b", line, re.IGNORECASE):
            current_tr069 = True
            continue
        tr069_ip_match = _TR069_IP_INDEX_RE.search(line)
        if tr069_ip_match:
            current_tr069_ip_index = int(tr069_ip_match.group(1))

    flush_current()
    return ParsedOltDumpProfiles(
        line_profiles=line_profiles,
        tr069_profiles=tr069_profiles,
        ont_line_profile_counts=ont_line_profile_counts,
    )


def audit_olt_config_pack_dump(
    db: Session,
    olt_id: str,
    *,
    dump_roots: tuple[Path, ...] = DEFAULT_DUMP_ROOTS,
) -> OltConfigPackDumpAudit:
    """Compare one OLT config pack with a local running-config dump."""
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return OltConfigPackDumpAudit(
            olt_id=str(olt_id),
            olt_name="unknown",
            dump_path=None,
            success=False,
            errors=["OLT device not found"],
        )

    dump_path = find_local_olt_dump(olt, dump_roots=dump_roots)
    audit = OltConfigPackDumpAudit(
        olt_id=str(olt.id),
        olt_name=olt.name or str(olt.id),
        dump_path=str(dump_path) if dump_path else None,
        success=False,
    )
    if dump_path is None:
        audit.errors.append("No local running-config dump found")
        return audit

    pack = resolve_olt_config_pack(db, str(olt.id))
    if pack is None:
        audit.errors.append("OLT config pack could not be resolved")
        return audit

    required = {
        "tr069_olt_profile_id": pack.tr069_olt_profile_id,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        audit.errors.append("Config pack missing fields: " + ", ".join(missing))
        return audit

    parsed = parse_olt_dump_profiles(dump_path.read_text(errors="replace"))
    tr069_profile_id = int(pack.tr069_olt_profile_id)
    audit.success = True
    audit.observed = {
        "tr069_profile_id": tr069_profile_id,
        "tr069_profile_exists": tr069_profile_id in parsed.tr069_profiles,
        "imported_line_profiles": sorted(parsed.line_profiles),
        "ont_line_profile_counts": dict(parsed.ont_line_profile_counts.most_common()),
    }

    if tr069_profile_id not in parsed.tr069_profiles:
        audit.errors.append(
            f"Config pack tr069_olt_profile_id={tr069_profile_id} was not found in dump"
        )

    audit.warnings.append(
        "Line/service profile and GEM validation is handled by imported OLT state; "
        "run scripts/import_olt_state.py and scripts/report_missing_olt_mappings.py."
    )
    return audit


def apply_dump_audit_suggestions(db: Session, audits: list[OltConfigPackDumpAudit]) -> int:
    """Deprecated no-op: profile defaults are no longer written to config_pack."""
    del db, audits
    return 0


def active_olt_ids(db: Session) -> list[str]:
    return [
        str(olt_id)
        for olt_id in db.scalars(
            select(OLTDevice.id)
            .where(OLTDevice.is_active.is_(True))
            .order_by(OLTDevice.name)
        ).all()
    ]
