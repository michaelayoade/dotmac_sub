"""Parse OLT profile definitions from backed-up running config.

This module extracts DBA profiles, traffic tables, line profiles, and service
profiles from stored running config text instead of live SSH sessions.

Advantages over live SSH:
- Faster (no SSH round-trip)
- More reliable (no timeout/connection issues)
- Consistent (uses already-captured config)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OltConfigBackup, OLTDevice

logger = logging.getLogger(__name__)

BACKUP_DIR = Path("/app/uploads/olt_config_backups")


@dataclass
class ParsedProfile:
    """A profile definition parsed from running config."""

    profile_id: int
    name: str
    profile_type: str  # dba, traffic, line, service
    raw_line: str


def get_latest_config_backup(db: Session, olt_id) -> OltConfigBackup | None:
    """Get most recent config backup for an OLT."""
    return db.scalar(
        select(OltConfigBackup)
        .where(OltConfigBackup.olt_device_id == olt_id)
        .order_by(OltConfigBackup.created_at.desc())
        .limit(1)
    )


def read_config_text(backup: OltConfigBackup) -> str | None:
    """Read config text from backup file."""
    if not backup or not backup.file_path:
        return None
    filepath = BACKUP_DIR / backup.file_path
    if not filepath.exists():
        logger.warning("Config backup file not found: %s", filepath)
        return None
    try:
        return filepath.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.error("Failed to read config file %s: %s", filepath, e)
        return None


def parse_dba_profiles(config_text: str) -> list[ParsedProfile]:
    """Extract DBA profile definitions.

    Format: dba-profile add profile-id 10 profile-name "MANAGEMENT" type1 fix 1024 ...
    """
    profiles = []
    pattern = r'dba-profile add profile-id (\d+) profile-name "([^"]+)"'
    for match in re.finditer(pattern, config_text, re.IGNORECASE):
        profile_id = int(match.group(1))
        name = match.group(2)
        # Get full line for raw_line
        start = config_text.rfind("\n", 0, match.start()) + 1
        end = config_text.find("\n", match.end())
        if end == -1:
            end = len(config_text)
        raw_line = config_text[start:end].strip()
        profiles.append(
            ParsedProfile(
                profile_id=profile_id,
                name=name,
                profile_type="dba",
                raw_line=raw_line,
            )
        )
    return profiles


def parse_traffic_tables(config_text: str) -> list[ParsedProfile]:
    """Extract traffic table definitions.

    Format: traffic table ip index 7 name "MANAGEMENT" cir 10240 ...
    """
    profiles = []
    pattern = r'traffic table ip index (\d+) name "([^"]+)"'
    for match in re.finditer(pattern, config_text, re.IGNORECASE):
        index = int(match.group(1))
        name = match.group(2)
        start = config_text.rfind("\n", 0, match.start()) + 1
        end = config_text.find("\n", match.end())
        if end == -1:
            end = len(config_text)
        raw_line = config_text[start:end].strip()
        profiles.append(
            ParsedProfile(
                profile_id=index,
                name=name,
                profile_type="traffic",
                raw_line=raw_line,
            )
        )
    return profiles


def parse_line_profiles(config_text: str) -> list[ParsedProfile]:
    """Extract line profile definitions.

    Format: ont-lineprofile gpon profile-id 1 profile-name "SPL_1_Unlimited_3"
    """
    profiles = []
    pattern = r'ont-lineprofile gpon profile-id (\d+) profile-name "([^"]+)"'
    for match in re.finditer(pattern, config_text, re.IGNORECASE):
        profile_id = int(match.group(1))
        name = match.group(2)
        start = config_text.rfind("\n", 0, match.start()) + 1
        end = config_text.find("\n", match.end())
        if end == -1:
            end = len(config_text)
        raw_line = config_text[start:end].strip()
        profiles.append(
            ParsedProfile(
                profile_id=profile_id,
                name=name,
                profile_type="line",
                raw_line=raw_line,
            )
        )
    return profiles


def parse_service_profiles(config_text: str) -> list[ParsedProfile]:
    """Extract service profile definitions.

    Format: ont-srvprofile gpon profile-id 1 profile-name "SPL_ONU_R"
    """
    profiles = []
    pattern = r'ont-srvprofile gpon profile-id (\d+) profile-name "([^"]+)"'
    for match in re.finditer(pattern, config_text, re.IGNORECASE):
        profile_id = int(match.group(1))
        name = match.group(2)
        start = config_text.rfind("\n", 0, match.start()) + 1
        end = config_text.find("\n", match.end())
        if end == -1:
            end = len(config_text)
        raw_line = config_text[start:end].strip()
        profiles.append(
            ParsedProfile(
                profile_id=profile_id,
                name=name,
                profile_type="service",
                raw_line=raw_line,
            )
        )
    return profiles


def get_profile_inventory_from_backup(
    db: Session,
    olt: OLTDevice,
) -> tuple[bool, str, dict[str, dict[int, str]]]:
    """Parse all profile types from OLT's latest config backup.

    Returns:
        Tuple of (success, message, inventory_dict)
        inventory_dict maps profile_type -> {profile_id: name}
    """
    backup = get_latest_config_backup(db, olt.id)
    if not backup:
        return False, f"No config backup found for OLT {olt.name}", {}

    config_text = read_config_text(backup)
    if not config_text:
        return False, f"Failed to read config backup for OLT {olt.name}", {}

    inventory: dict[str, dict[int, str]] = {
        "dba": {},
        "traffic": {},
        "line": {},
        "service": {},
    }

    for profile in parse_dba_profiles(config_text):
        inventory["dba"][profile.profile_id] = profile.name

    for profile in parse_traffic_tables(config_text):
        inventory["traffic"][profile.profile_id] = profile.name

    for profile in parse_line_profiles(config_text):
        inventory["line"][profile.profile_id] = profile.name

    for profile in parse_service_profiles(config_text):
        inventory["service"][profile.profile_id] = profile.name

    total = sum(len(v) for v in inventory.values())
    backup_age = backup.created_at.isoformat() if backup.created_at else "unknown"
    message = (
        f"Parsed {total} profiles from backup ({backup_age}): "
        f"{len(inventory['dba'])} DBA, {len(inventory['traffic'])} traffic, "
        f"{len(inventory['line'])} line, {len(inventory['service'])} service"
    )
    return True, message, inventory


def check_bundle_drift_from_backup(
    db: Session,
    olt: OLTDevice,
    expected_profiles: dict[str, dict[str, dict[str, object]]],
) -> tuple[str, dict[str, object]]:
    """Check if expected bundle profiles exist in backup inventory.

    Args:
        db: Database session
        olt: OLT device
        expected_profiles: Dict from _expected_bundle_inventory() format:
            {category: {label: {id: X, name: Y}}}

    Returns:
        Tuple of (status, details) where status is "in_sync", "drifted", or "drift_unknown"
    """
    ok, message, inventory = get_profile_inventory_from_backup(db, olt)
    if not ok:
        return "drift_unknown", {"message": message, "errors": [message]}

    missing: list[str] = []
    mismatched: list[str] = []

    for category, items in expected_profiles.items():
        live_items = inventory.get(category, {})
        for label, profile in items.items():
            profile_id = int(profile["id"])  # type: ignore[call-overload]
            expected_name = str(profile.get("name") or "").strip()
            live_name = live_items.get(profile_id)

            if live_name is None:
                missing.append(f"{label} {profile_id}")
                continue
            if expected_name and live_name and live_name != expected_name:
                mismatched.append(
                    f"{label} {profile_id}: expected {expected_name}, got {live_name}"
                )

    if missing or mismatched:
        return "drifted", {
            "message": "Backup profiles differ from saved bundle",
            "missing": missing,
            "mismatched": mismatched,
            "source": "config_backup",
        }
    return "in_sync", {
        "message": "Backup profiles match saved bundle IDs and names",
        "source": "config_backup",
    }
