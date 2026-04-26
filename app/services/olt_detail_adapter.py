"""Application-facing adapter for OLT detail page data."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services.adapters import adapter_registry

logger = logging.getLogger(__name__)

ALLOWED_CLI_PREFIXES = ("display", "show", "ping", "traceroute")
QUICK_CLI_COMMANDS = (
    "display version",
    "display ont info 0 all",
    "display interface brief",
    "display board 0",
    "display cpu",
    "display memory",
    "display alarm active all",
    "display ont optical-info 0 all",
)


class OltDetailAdapter:
    """Expose OLT detail context through a single UI boundary."""

    name = "olt_detail"

    def page_data(self, db: Session, *, olt_id: str) -> dict[str, object] | None:
        from app.services import audit_helpers
        from app.services import web_network_core_devices as core_devices_service
        from app.services import web_network_operations as operations_service
        from app.services.network import olt_operations as olt_operations_service
        from app.services.network import olt_tr069_admin as olt_tr069_admin_service

        page_data = core_devices_service.olt_detail_page_data(db, olt_id)
        if not page_data:
            return None

        olt = page_data.get("olt")
        monitoring_device = page_data.get("monitoring_device")

        try:
            operations = operations_service.build_operation_history(
                db, "olt", str(olt_id)
            )
        except Exception:
            logger.error(
                "Failed to load operation history for OLT %s", olt_id, exc_info=True
            )
            operations = []

        try:
            activities = audit_helpers.build_audit_activities(db, "olt", str(olt_id))
        except Exception:
            logger.error("Failed to load audit activity for OLT %s", olt_id, exc_info=True)
            activities = []
        available_firmware = olt_operations_service.get_olt_firmware_images(db, olt_id)

        page_data.update(
            {
                "activities": activities,
                "operations": operations,
                "available_olt_firmware": available_firmware,
                "acs_prefill": self._build_acs_prefill(olt),
                "operational_acs_server": (
                    olt_tr069_admin_service.resolve_operational_acs_server(db, olt=olt)
                ),
                "access_info": self._build_access_info(olt, monitoring_device),
                "monitoring_source": self._build_monitoring_source(page_data),
                "detail_actions": self._build_detail_actions(olt_id, olt),
                "terminal_context": self._build_terminal_context(olt_id),
                "firmware_context": self._build_firmware_context(
                    olt, available_firmware
                ),
                "config_context": self._build_config_context(page_data),
                "ont_relationship_context": self._build_ont_relationship_context(
                    page_data
                ),
            }
        )
        return page_data

    def events_context(self, db: Session, *, olt_id: str) -> dict[str, object]:
        from app.services.network import olt_web_resources as olt_web_resources_service

        return olt_web_resources_service.olt_device_events_context(db, olt_id)

    def _build_acs_prefill(self, olt: object | None) -> dict[str, str]:
        acs_prefill: dict[str, str] = {}
        acs = getattr(olt, "tr069_acs_server", None) if olt else None
        if acs is not None:
            acs_prefill = {
                "cwmp_url": getattr(acs, "cwmp_url", "") or "",
                "cwmp_username": getattr(acs, "cwmp_username", "") or "",
            }
        return acs_prefill

    def _build_access_info(
        self, olt: object | None, monitoring_device: object | None
    ) -> dict[str, object]:
        snmp_enabled = bool(getattr(monitoring_device, "snmp_enabled", False))
        snmp_version = getattr(monitoring_device, "snmp_version", None)
        snmp_credential_saved = False
        snmp_credential_label = "Credential"
        if snmp_version == "v2c":
            snmp_credential_label = "Community"
            snmp_credential_saved = bool(
                getattr(monitoring_device, "snmp_community", None)
            )
        elif snmp_version == "v3":
            snmp_credential_label = "User"
            snmp_credential_saved = bool(
                getattr(monitoring_device, "snmp_username", None)
            )

        return {
            "ssh": {
                "username": getattr(olt, "ssh_username", None) or "Not configured",
                "port": getattr(olt, "ssh_port", None) or 22,
                "password_status": (
                    "Saved" if getattr(olt, "ssh_password", None) else "Not configured"
                ),
            },
            "netconf": {
                "enabled": bool(getattr(olt, "netconf_enabled", False)),
                "port": getattr(olt, "netconf_port", None),
            },
            "snmp": {
                "linked": monitoring_device is not None,
                "enabled": snmp_enabled,
                "port": getattr(monitoring_device, "snmp_port", None),
                "version": snmp_version,
                "credential_label": snmp_credential_label,
                "credential_status": "Saved" if snmp_credential_saved else "Not saved",
            },
        }

    def _build_monitoring_source(
        self, page_data: dict[str, object]
    ) -> dict[str, object]:
        resolution = page_data.get("monitoring_resolution")
        resolution = resolution if isinstance(resolution, dict) else {}
        monitoring_device = page_data.get("monitoring_device")
        return {
            "linked": monitoring_device is not None,
            "source": "network_device" if monitoring_device is not None else "olt_device",
            "match_strategy": resolution.get("match_strategy", "unknown"),
            "authoritative": bool(resolution.get("authoritative", False)),
            "warning": resolution.get("warning"),
        }

    def _build_detail_actions(
        self, olt_id: str, olt: object | None
    ) -> dict[str, dict[str, Any]]:
        base = f"/admin/network/olts/{olt_id}"
        return {
            "header": {
                "autofind": {"url": f"{base}/autofind", "visible": True},
                "init_tr069": {"url": f"{base}/init-tr069", "visible": True},
                "edit": {"url": f"{base}/edit", "visible": True},
            },
            "sidebar": {
                "edit": {"url": f"{base}/edit", "method": "GET", "visible": True},
                "onts": {
                    "url": f"/admin/network/onts?olt_id={olt_id}",
                    "method": "GET",
                    "visible": True,
                },
                "test_ssh": {
                    "url": f"{base}/test-ssh",
                    "method": "POST",
                    "visible": True,
                },
                "test_snmp": {
                    "url": f"{base}/test-snmp",
                    "method": "POST",
                    "visible": True,
                },
                "test_netconf": {
                    "url": f"{base}/test-netconf",
                    "method": "POST",
                    "visible": bool(getattr(olt, "netconf_enabled", False)),
                },
                "sync_onts": {
                    "url": f"{base}/sync-onts",
                    "method": "POST",
                    "visible": True,
                },
                "repair_pon_ports": {
                    "url": f"{base}/repair-pon-ports",
                    "method": "POST",
                    "visible": True,
                },
                "backup_create": {
                    "url": f"{base}/backups/ssh-backup",
                    "method": "POST",
                    "visible": True,
                },
                "backup_history": {
                    "url": f"{base}/backups",
                    "method": "GET",
                    "visible": True,
                },
                "firmware_upgrade": {
                    "url": f"{base}/firmware-upgrade",
                    "method": "POST",
                    "visible": True,
                },
            },
        }

    def _build_terminal_context(self, olt_id: str) -> dict[str, object]:
        base = f"/admin/network/olts/{olt_id}"
        return {
            "allowed_prefixes": list(ALLOWED_CLI_PREFIXES),
            "quick_commands": list(QUICK_CLI_COMMANDS),
            "actions": {
                "cli": f"{base}/cli",
                "ont_status_by_serial": f"{base}/ont-status-by-serial",
                "ssh_get_config": f"{base}/ssh-get-config",
                "ssh_backup": f"{base}/backups/ssh-backup",
                "netconf_get_config": f"{base}/netconf-get-config",
            },
        }

    def _build_firmware_context(
        self, olt: object | None, available_firmware: list[object]
    ) -> dict[str, object]:
        return {
            "current_version": getattr(olt, "firmware_version", None) or "Unknown",
            "software_version": getattr(olt, "software_version", None),
            "vendor": getattr(olt, "vendor", None),
            "images": available_firmware,
        }

    def _build_config_context(self, page_data: dict[str, object]) -> dict[str, object]:
        return {
            "vlans": page_data.get("olt_vlans", []),
            "available_vlans": page_data.get("available_vlans", []),
            "ip_pool_usage": page_data.get("olt_ip_pool_usage", []),
            "available_ip_pools": page_data.get("available_ip_pools", []),
        }

    def _build_ont_relationship_context(
        self, page_data: dict[str, object]
    ) -> dict[str, object]:
        olt = page_data.get("olt")
        olt_id = getattr(olt, "id", None)
        assignment_by_ont_id = page_data.get("assignment_by_ont_id", {}) or {}
        signal_data = page_data.get("signal_data", {}) or {}
        pon_port_display_by_ont_id = page_data.get("pon_port_display_by_ont_id", {}) or {}
        ont_mac_by_ont_id = page_data.get("ont_mac_by_ont_id", {}) or {}

        rows: list[dict[str, object]] = []
        mismatches = 0
        direct_only = 0
        assignment_only = 0
        direct_and_assignment = 0

        for ont in page_data.get("onts_on_olt", []) or []:
            ont_id = str(getattr(ont, "id", "") or "")
            if not ont_id:
                continue

            assignment = assignment_by_ont_id.get(ont_id)
            pon_port = getattr(assignment, "pon_port", None) if assignment else None
            assigned_olt_id = getattr(pon_port, "olt_id", None) if pon_port else None
            direct_olt_id = getattr(ont, "olt_device_id", None)
            direct_match = bool(olt_id and direct_olt_id and direct_olt_id == olt_id)
            assignment_match = bool(olt_id and assigned_olt_id and assigned_olt_id == olt_id)
            mismatch = bool(direct_olt_id and assigned_olt_id and direct_olt_id != assigned_olt_id)

            if mismatch:
                mismatches += 1
                source = "mismatch"
                source_label = "Mismatch"
            elif direct_match and assignment_match:
                source = "direct+assignment"
                source_label = "Direct + assigned"
                direct_and_assignment += 1
            elif assignment_match:
                source = "assignment"
                source_label = "Assigned port"
                assignment_only += 1
            elif direct_match:
                source = "direct"
                source_label = "Direct OLT"
                direct_only += 1
            else:
                source = "unknown"
                source_label = "Unresolved"

            mac_display = str(ont_mac_by_ont_id.get(ont_id) or "").strip()
            if not mac_display:
                mac_display = str(getattr(ont, "serial_number", "") or "").strip()
            port_display = pon_port_display_by_ont_id.get(ont_id)
            if not port_display and pon_port is not None:
                port_display = getattr(pon_port, "notes", None) or getattr(
                    pon_port, "name", None
                )
            if not port_display and getattr(ont, "board", None) and getattr(
                ont, "port", None
            ):
                port_display = f"{ont.board}/{ont.port}"

            rows.append(
                {
                    "ont": ont,
                    "id": ont_id,
                    "url": f"/admin/network/onts/{ont_id}",
                    "identity": mac_display,
                    "port_display": port_display,
                    "assignment": assignment,
                    "relationship_source": source,
                    "relationship_label": source_label,
                    "relationship_mismatch": mismatch,
                    "signal": signal_data.get(ont_id, {}),
                    "search_text": f"{mac_display} {ont_id}".lower(),
                }
            )

        return {
            "rows": rows,
            "summary": {
                "total": len(rows),
                "direct_only": direct_only,
                "assignment_only": assignment_only,
                "direct_and_assignment": direct_and_assignment,
                "mismatches": mismatches,
            },
        }


olt_detail_adapter = OltDetailAdapter()
adapter_registry.register(olt_detail_adapter)
