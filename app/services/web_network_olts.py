"""Compatibility facade for OLT web service helpers.

The OLT web service was split into focused modules under ``app.services.network``.
Some callers still import this historical module path, so keep a thin facade here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import OltConfigBackup, OLTDevice
from app.schemas.network import OLTDeviceCreate, OLTDeviceUpdate
from app.services.network import olt_operations as _operations
from app.services.network import olt_web_forms as _forms

backup_file_path = _operations.backup_file_path
compare_olt_backups = _operations.compare_olt_backups
list_olt_backups = _operations.list_olt_backups
read_backup_preview = _operations.read_backup_preview
restore_from_backup = _operations.restore_from_backup
test_olt_ssh_connection = _operations.test_olt_ssh_connection
_extract_firmware_version = _operations.extract_firmware_version

build_form_model = _forms.build_form_model
create_olt = _forms.create_olt
create_olt_with_audit = _forms.create_olt_with_audit
parse_form_values = _forms.parse_form_values
snapshot = _forms.snapshot
update_olt = _forms.update_olt
update_olt_with_audit = _forms.update_olt_with_audit
validate_values = _forms.validate_values
_queue_acs_propagation = _forms._queue_acs_propagation


def fetch_running_config(olt: OLTDevice, db: Session | None = None) -> str | None:
    """Fetch OLT running config."""
    return _operations.fetch_running_config(olt, db=db)


def test_olt_connection(db: Session, olt_id: str) -> tuple[bool, str]:
    """Test OLT connection by fetching config."""
    olt = _forms.get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"
    if not olt.mgmt_ip:
        return False, "Management IP is required"
    config = _operations.fetch_running_config(olt)
    if not config:
        return False, "Connection test failed: unable to fetch SNMP data"
    return True, "Connection test successful"


def run_test_backup(db: Session, olt_id: str) -> tuple[OltConfigBackup | None, str]:
    """Run a test backup for an OLT."""
    return _operations.run_test_backup(db, olt_id)


def create_payload(values: dict[str, object]) -> OLTDeviceCreate:
    """Build create payload from form values."""
    return _forms.create_payload(values)


def update_payload(values: dict[str, object]) -> OLTDeviceUpdate:
    """Build update payload from form values."""
    return _forms.update_payload(values)


def sync_monitoring_device(
    db: Session, olt: OLTDevice, values: Mapping[str, Any]
) -> None:
    """Sync OLT with monitoring system."""
    return _forms.sync_monitoring_device(db, olt, dict(values))
