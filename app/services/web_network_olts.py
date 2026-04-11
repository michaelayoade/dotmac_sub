"""Service helpers for admin OLT web routes."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.credential_crypto import (
    encrypt_credential as _default_encrypt_credential,
)
from app.services.network import olt_autofind as olt_autofind_service
from app.services.network import olt_operations as olt_operations_service
from app.services.network import olt_snmp_sync as olt_snmp_sync_service
from app.services.network import olt_ssh as olt_ssh_service
from app.services.network import olt_ssh_service_ports as olt_service_ports_service
from app.services.network import olt_tr069_admin as olt_tr069_admin_service
from app.services.network import olt_web_forms as olt_web_forms_service
from app.services.network import olt_web_resources as olt_web_resources_service
from app.services.network import olt_web_serials as olt_web_serials_service
from app.services.network import olt_web_topology as olt_web_topology_service

logger = logging.getLogger(__name__)
encrypt_credential = _default_encrypt_credential


def _call_form_helper(fn, *args):
    original_encrypt = olt_web_forms_service.encrypt_credential
    original_sync = olt_web_forms_service.sync_monitoring_device
    original_queue = olt_web_forms_service._queue_acs_propagation
    olt_web_forms_service.encrypt_credential = encrypt_credential
    olt_web_forms_service.sync_monitoring_device = sync_monitoring_device
    olt_web_forms_service._queue_acs_propagation = _queue_acs_propagation
    try:
        return fn(*args)
    finally:
        olt_web_forms_service.encrypt_credential = original_encrypt
        olt_web_forms_service.sync_monitoring_device = original_sync
        olt_web_forms_service._queue_acs_propagation = original_queue


def _encrypt_if_set(values, key):
    return _call_form_helper(olt_web_forms_service._encrypt_if_set, values, key)


def create_payload(values):
    return _call_form_helper(olt_web_forms_service.create_payload, values)


def update_payload(values):
    return _call_form_helper(olt_web_forms_service.update_payload, values)


def create_olt(db, values):
    return _call_form_helper(olt_web_forms_service.create_olt, db, values)


def update_olt(db, olt_id, values):
    return _call_form_helper(olt_web_forms_service.update_olt, db, olt_id, values)


def create_olt_with_audit(db, request, values, actor_id):
    return _call_form_helper(
        olt_web_forms_service.create_olt_with_audit, db, request, values, actor_id
    )


def update_olt_with_audit(db, request, olt_id, before_obj, values, actor_id):
    return _call_form_helper(
        olt_web_forms_service.update_olt_with_audit,
        db,
        request,
        olt_id,
        before_obj,
        values,
        actor_id,
    )

# Compatibility exports for OLT form helpers now owned by network/olt_web_forms.py.
_find_linked_network_device = olt_web_forms_service._find_linked_network_device
build_form_model = olt_web_forms_service.build_form_model
get_olt_or_none = olt_web_forms_service.get_olt_or_none
integrity_error_message = olt_web_forms_service.integrity_error_message
parse_form_values = olt_web_forms_service.parse_form_values
snapshot = olt_web_forms_service.snapshot
sync_monitoring_device = olt_web_forms_service.sync_monitoring_device
validate_values = olt_web_forms_service.validate_values

# Compatibility exports for OLT resource/event helpers.
assign_ip_pool_to_olt = olt_web_resources_service.assign_ip_pool_to_olt
assign_vlan_to_olt = olt_web_resources_service.assign_vlan_to_olt
available_ip_pools_for_olt = olt_web_resources_service.available_ip_pools_for_olt
available_vlans_for_olt = olt_web_resources_service.available_vlans_for_olt
olt_device_events_context = olt_web_resources_service.olt_device_events_context
unassign_ip_pool_from_olt = olt_web_resources_service.unassign_ip_pool_from_olt
unassign_vlan_from_olt = olt_web_resources_service.unassign_vlan_from_olt

# Compatibility exports for ONT serial matching helpers.
_is_plausible_vendor_serial = olt_web_serials_service._is_plausible_vendor_serial
_looks_synthetic_ont_serial = olt_web_serials_service._looks_synthetic_ont_serial
_normalize_ont_serial = olt_web_serials_service._normalize_ont_serial
_prefer_ont_candidate = olt_web_serials_service._prefer_ont_candidate

# Compatibility exports for PON topology helpers.
_card_port_fsp = olt_web_topology_service._card_port_fsp
_ensure_canonical_pon_port = olt_web_topology_service._ensure_canonical_pon_port
_infer_pon_repair_target = olt_web_topology_service._infer_pon_repair_target
_olt_sync_lock_key = olt_web_topology_service._olt_sync_lock_key
_resolve_pon_card_port = olt_web_topology_service._resolve_pon_card_port
_retire_duplicate_pon_port = olt_web_topology_service._retire_duplicate_pon_port
ensure_canonical_pon_port = olt_web_topology_service.ensure_canonical_pon_port
repair_pon_ports_for_olt = olt_web_topology_service.repair_pon_ports_for_olt
repair_pon_ports_for_olt_tracked = (
    olt_web_topology_service.repair_pon_ports_for_olt_tracked
)

_parse_fsp_parts = olt_autofind_service.parse_fsp_parts
_persist_authorized_ont_inventory = (
    olt_autofind_service.persist_authorized_ont_inventory
)
get_autofind_onts = olt_autofind_service.get_autofind_onts


def authorize_autofind_ont(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    force_reauthorize: bool = False,
) -> tuple[bool, str, str]:
    """Authorize an unregistered ONT via the tracked workflow.

    Args:
        db: Database session
        olt_id: UUID of the OLT
        fsp: Frame/Slot/Port (e.g., "0/1/13")
        serial_number: ONT serial number
        force_reauthorize: If True, delete any existing registration of this
            serial before authorizing.
    """
    from app.services.network import (
        olt_authorization_workflow as olt_authorization_workflow_service,
    )

    if force_reauthorize:
        result = olt_authorization_workflow_service.authorize_autofind_ont(
            db,
            olt_id,
            fsp,
            serial_number,
            force_reauthorize=True,
        )
    else:
        result = olt_authorization_workflow_service.authorize_autofind_ont(
            db,
            olt_id,
            fsp,
            serial_number,
        )
    return result.success, result.status, result.message


clone_service_ports = olt_service_ports_service.clone_service_ports
provision_ont_service_ports = olt_service_ports_service.provision_ont_service_ports


# Compatibility exports for OLT operations now owned by network/olt_operations.py.
_olt_backup_base_dir = olt_operations_service.olt_backup_base_dir
_resolve_backup_file = olt_operations_service.resolve_backup_file
list_olt_backups = olt_operations_service.list_olt_backups
get_olt_backup_or_none = olt_operations_service.get_olt_backup_or_none
backup_file_path = olt_operations_service.backup_file_path
read_backup_preview = olt_operations_service.read_backup_preview
read_backup_content = olt_operations_service.read_backup_content
compare_olt_backups = olt_operations_service.compare_olt_backups
fetch_running_config = olt_operations_service.fetch_running_config
_extract_firmware_version = olt_operations_service.extract_firmware_version
validate_cli_command = olt_operations_service.validate_cli_command


def _call_operations_helper(fn, *args, **kwargs):
    original_fetch = olt_operations_service.fetch_running_config
    original_get_olt = olt_operations_service.get_olt_or_none
    original_ssh = olt_operations_service.olt_ssh_service
    olt_operations_service.fetch_running_config = fetch_running_config
    olt_operations_service.get_olt_or_none = get_olt_or_none
    olt_operations_service.olt_ssh_service = olt_ssh_service
    try:
        return fn(*args, **kwargs)
    finally:
        olt_operations_service.fetch_running_config = original_fetch
        olt_operations_service.get_olt_or_none = original_get_olt
        olt_operations_service.olt_ssh_service = original_ssh


def test_olt_connection(db: Session, olt_id: str) -> tuple[bool, str]:
    return _call_operations_helper(olt_operations_service.test_olt_connection, db, olt_id)


def test_olt_snmp_connection(db: Session, olt_id: str) -> tuple[bool, str]:
    return _call_operations_helper(
        olt_operations_service.test_olt_snmp_connection, db, olt_id
    )


def test_olt_ssh_connection(
    db: Session, olt_id: str
) -> tuple[bool, str, str | None]:
    return _call_operations_helper(
        olt_operations_service.test_olt_ssh_connection, db, olt_id
    )


def test_olt_netconf_connection(
    db: Session, olt_id: str
) -> tuple[bool, str, list[str]]:
    return _call_operations_helper(
        olt_operations_service.test_olt_netconf_connection, db, olt_id
    )


def get_olt_netconf_config(
    db: Session, olt_id: str, *, filter_xpath: str | None = None
) -> tuple[bool, str, str]:
    return _call_operations_helper(
        olt_operations_service.get_olt_netconf_config,
        db,
        olt_id,
        filter_xpath=filter_xpath,
    )


def get_olt_firmware_images(db: Session, olt_id: str) -> list:
    return _call_operations_helper(
        olt_operations_service.get_olt_firmware_images, db, olt_id
    )


def trigger_olt_firmware_upgrade(
    db: Session, olt_id: str, image_id: str
) -> tuple[bool, str]:
    return _call_operations_helper(
        olt_operations_service.trigger_olt_firmware_upgrade, db, olt_id, image_id
    )


def run_test_backup(db: Session, olt_id: str):
    return _call_operations_helper(olt_operations_service.run_test_backup, db, olt_id)


def execute_cli_command(
    db: Session, olt_id: str, command: str
) -> tuple[bool, str, str]:
    return _call_operations_helper(
        olt_operations_service.execute_cli_command, db, olt_id, command
    )


def backup_running_config_ssh(db: Session, olt_id: str):
    return _call_operations_helper(
        olt_operations_service.backup_running_config_ssh, db, olt_id
    )


# Compatibility exports for bulk SNMP ONT discovery now owned by
# network/olt_snmp_sync.py.
_parse_walk_composite = olt_snmp_sync_service._parse_walk_composite
_parse_signal_dbm = olt_snmp_sync_service._parse_signal_dbm
_parse_distance_m = olt_snmp_sync_service._parse_distance_m
_parse_online_status = olt_snmp_sync_service._parse_online_status
_run_simple_v2c_walk = olt_snmp_sync_service._run_simple_v2c_walk
_sync_onts_from_olt_snmp_impl = olt_snmp_sync_service._sync_onts_from_olt_snmp_impl
sync_onts_from_olt_snmp = olt_snmp_sync_service.sync_onts_from_olt_snmp
sync_onts_from_olt_snmp_tracked = (
    olt_snmp_sync_service.sync_onts_from_olt_snmp_tracked
)


# Compatibility exports for OLT TR-069 profile/admin logic now owned by
# network/olt_tr069_admin.py.
resolve_operational_acs_server = olt_tr069_admin_service.resolve_operational_acs_server
ensure_tr069_profile_for_linked_acs = (
    olt_tr069_admin_service.ensure_tr069_profile_for_linked_acs
)
get_tr069_profiles_context = olt_tr069_admin_service.get_tr069_profiles_context
handle_create_tr069_profile = olt_tr069_admin_service.handle_create_tr069_profile
handle_rebind_tr069_profiles = olt_tr069_admin_service.handle_rebind_tr069_profiles
_queue_acs_propagation = olt_tr069_admin_service.queue_acs_propagation
