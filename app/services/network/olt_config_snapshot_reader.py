"""Snapshot-first reader for static OLT running configuration.

Static OLT configuration reads should come from one captured running-config
backup. This avoids mixed evidence from multiple live SSH commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.models.network import OltConfigBackup, OLTDevice
from app.services.network import olt_operations
from app.services.network.olt_config_audit import parse_huawei_running_config
from app.services.network.olt_config_profile_parser import (
    parse_dba_profiles,
    parse_line_profiles,
    parse_service_profiles,
    parse_traffic_tables,
)
from app.services.network.profile_inventory_preflight import ProfileInventory


class BackupRunner(Protocol):
    def __call__(
        self, db: Session, olt_id: str
    ) -> tuple[OltConfigBackup | None, str]: ...


@dataclass(frozen=True)
class OltConfigSnapshot:
    backup: OltConfigBackup
    config_text: str

    @property
    def backup_id(self) -> str:
        return str(self.backup.id)

    @property
    def captured_at(self):
        return self.backup.created_at

    def provenance(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "captured_at": self.captured_at.isoformat()
            if self.captured_at
            else None,
            "source": "running_config_backup",
        }


@dataclass(frozen=True)
class OltConfigSnapshotReader:
    db: Session
    olt: OLTDevice
    snapshot: OltConfigSnapshot

    @classmethod
    def capture(
        cls,
        db: Session,
        olt: OLTDevice,
        *,
        backup_runner: BackupRunner = olt_operations.backup_running_config_ssh,
    ) -> tuple[OltConfigSnapshotReader | None, str]:
        backup, message = backup_runner(db, str(olt.id))
        if backup is None:
            return None, message
        try:
            config_text = olt_operations.read_backup_content(backup)
        except Exception as exc:
            return None, f"Backup captured but could not be read: {exc}"
        return cls(db, olt, OltConfigSnapshot(backup=backup, config_text=config_text)), message

    @classmethod
    def latest(cls, db: Session, olt: OLTDevice) -> tuple[OltConfigSnapshotReader | None, str]:
        from app.services.network.olt_config_audit import latest_valid_backup

        backup = latest_valid_backup(db, olt.id)
        if backup is None:
            return None, f"No valid running-config backup found for OLT {olt.name}"
        try:
            config_text = olt_operations.read_backup_content(backup)
        except Exception as exc:
            return None, f"Latest backup could not be read: {exc}"
        return cls(db, olt, OltConfigSnapshot(backup=backup, config_text=config_text)), "Loaded latest running-config backup"

    def profile_inventory(self) -> dict[str, dict[int, str]]:
        inventory: dict[str, dict[int, str]] = {
            "dba": {},
            "traffic": {},
            "line": {},
            "service": {},
        }
        for profile in parse_dba_profiles(self.snapshot.config_text):
            inventory["dba"][profile.profile_id] = profile.name
        for profile in parse_traffic_tables(self.snapshot.config_text):
            inventory["traffic"][profile.profile_id] = profile.name
        for profile in parse_line_profiles(self.snapshot.config_text):
            inventory["line"][profile.profile_id] = profile.name
        for profile in parse_service_profiles(self.snapshot.config_text):
            inventory["service"][profile.profile_id] = profile.name
        return inventory

    def profile_preflight_inventory(self) -> ProfileInventory:
        inventory = self.profile_inventory()
        return ProfileInventory(
            dba_profile_ids=frozenset(inventory["dba"]),
            dba_profile_names=frozenset(inventory["dba"].values()),
            traffic_table_ids=frozenset(inventory["traffic"]),
            traffic_table_names=frozenset(inventory["traffic"].values()),
            line_profile_ids=frozenset(inventory["line"]),
            line_profile_names=frozenset(inventory["line"].values()),
            service_profile_ids=frozenset(inventory["service"]),
            service_profile_names=frozenset(inventory["service"].values()),
        )

    def service_ports(self):
        return parse_huawei_running_config(self.snapshot.config_text).service_ports

    def ont_registrations(self):
        return parse_huawei_running_config(self.snapshot.config_text).ont_registrations

    def validate_profile_bundle(
        self,
        expected_profiles: dict[str, dict[str, dict[str, object]]],
    ) -> tuple[str, dict[str, object]]:
        inventory = self.profile_inventory()
        missing: list[str] = []
        mismatched: list[str] = []

        for category, items in expected_profiles.items():
            snapshot_items = inventory.get(category, {})
            for label, profile in items.items():
                profile_id = int(profile["id"])  # type: ignore[call-overload]
                expected_name = str(profile.get("name") or "").strip()
                observed_name = snapshot_items.get(profile_id)
                if observed_name is None:
                    missing.append(f"{label} {profile_id}")
                    continue
                if expected_name and observed_name and observed_name != expected_name:
                    mismatched.append(
                        f"{label} {profile_id}: expected {expected_name}, got {observed_name}"
                    )

        details: dict[str, object] = {
            **self.snapshot.provenance(),
            "missing": missing,
            "mismatched": mismatched,
        }
        if missing or mismatched:
            details["message"] = "Snapshot profiles differ from saved bundle"
            return "drifted", details
        details["message"] = "Snapshot profiles match saved bundle IDs and names"
        return "in_sync", details
