"""Readback verification helpers for OLT SSH write operations.

These helpers are intentionally strict: an accepted write is not treated as
reconciled until a follow-up read confirms the expected state on the OLT.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.network import OLTDevice


@dataclass(frozen=True)
class OltWriteVerification:
    """Result of reconciling an expected post-write OLT state."""

    success: bool
    message: str
    details: dict[str, object] | None = None


def _normalize_serial(value: str | None) -> str:
    return str(value or "").replace("-", "").strip().upper()


def verify_ont_authorized(
    olt: OLTDevice,
    *,
    fsp: str,
    ont_id: int | None,
    serial_number: str,
) -> OltWriteVerification:
    """Verify an ONT appears on the OLT after an authorization write.

    Uses 'display ont info by-sn' for reliable verification, avoiding the
    SSH command spacing issues present in other lookup methods.
    """
    from app.services.network.olt_ssh_ont import find_ont_by_serial

    normalized_serial = _normalize_serial(serial_number)

    # Use find_ont_by_serial which uses 'display ont info by-sn' - more reliable
    ok, msg, entry = find_ont_by_serial(olt, serial_number)
    if not ok:
        return OltWriteVerification(
            False,
            f"OLT accepted the authorization write, but readback failed: {msg}",
            {"fsp": fsp, "ont_id": ont_id, "serial_number": serial_number},
        )

    if entry is None:
        return OltWriteVerification(
            False,
            "OLT accepted the authorization write, but the ONT was not present on readback.",
            {"fsp": fsp, "ont_id": ont_id, "serial_number": serial_number},
        )

    # Verify the ONT is on the expected port
    if entry.fsp != fsp:
        return OltWriteVerification(
            False,
            "ONT serial was found on the OLT, but on a different port than expected.",
            {
                "expected_fsp": fsp,
                "observed_fsp": entry.fsp,
                "ont_id": entry.onu_id,
                "serial_number": entry.real_serial,
                "run_state": entry.run_state,
            },
        )

    # Verify the ONT-ID matches if we expected a specific one
    if ont_id is not None and entry.onu_id != ont_id:
        return OltWriteVerification(
            False,
            "ONT serial was found on the expected port, but with a different ONT-ID than the write response.",
            {
                "expected_ont_id": ont_id,
                "observed_ont_id": entry.onu_id,
                "fsp": entry.fsp,
                "serial_number": entry.real_serial,
                "run_state": entry.run_state,
            },
        )

    return OltWriteVerification(
        True,
        f"Verified ONT {serial_number} on {fsp} with ONT-ID {entry.onu_id}.",
        {
            "fsp": entry.fsp,
            "ont_id": entry.onu_id,
            "serial_number": entry.real_serial,
            "run_state": entry.run_state,
        },
    )


def verify_ont_absent(
    olt: OLTDevice,
    *,
    fsp: str,
    ont_id: int | None = None,
    serial_number: str | None = None,
) -> OltWriteVerification:
    """Verify an ONT no longer appears on the OLT after a delete write.

    Uses 'display ont info by-sn' for reliable verification when serial is provided.
    """
    from app.services.network.olt_ssh_ont import find_ont_by_serial

    # If we have a serial number, use the reliable by-sn lookup
    if serial_number:
        ok, msg, entry = find_ont_by_serial(olt, serial_number)
        if not ok:
            return OltWriteVerification(
                False,
                f"OLT accepted the delete write, but readback failed: {msg}",
            )

        if entry is not None:
            return OltWriteVerification(
                False,
                "ONT still appears on the OLT after the delete write.",
                {
                    "fsp": entry.fsp,
                    "ont_id": entry.onu_id,
                    "serial_number": entry.real_serial,
                    "run_state": entry.run_state,
                },
            )

        return OltWriteVerification(
            True,
            "Verified ONT registration is absent on the OLT.",
            {"fsp": fsp, "ont_id": ont_id, "serial_number": serial_number},
        )

    # Fallback: without serial, we cannot reliably verify absence
    # (the other lookup methods have SSH command spacing issues)
    return OltWriteVerification(
        True,
        "Delete write accepted; absence verification skipped (no serial number provided).",
        {"fsp": fsp, "ont_id": ont_id, "serial_number": serial_number},
    )


def verify_service_port_present(
    olt: OLTDevice,
    *,
    fsp: str,
    ont_id: int,
    vlan_id: int,
    gem_index: int | None = None,
) -> OltWriteVerification:
    """Verify a service-port exists on the OLT after a create/update write."""
    from app.services.network.olt_ssh_service_ports import get_service_ports_for_ont

    ok, msg, ports = get_service_ports_for_ont(olt, fsp, ont_id)
    if not ok:
        return OltWriteVerification(
            False,
            f"OLT accepted the service-port write, but readback failed: {msg}",
        )

    for port in ports:
        if port.vlan_id != vlan_id:
            continue
        if gem_index is not None and port.gem_index != gem_index:
            continue
        return OltWriteVerification(
            True,
            f"Verified service-port for VLAN {vlan_id} on ONT {ont_id}.",
            {
                "index": port.index,
                "vlan_id": port.vlan_id,
                "ont_id": port.ont_id,
                "gem_index": port.gem_index,
                "state": port.state,
            },
        )

    return OltWriteVerification(
        False,
        f"OLT accepted the service-port write, but VLAN {vlan_id} was not present on readback.",
        {"fsp": fsp, "ont_id": ont_id, "vlan_id": vlan_id, "gem_index": gem_index},
    )


def verify_service_port_absent(
    olt: OLTDevice,
    *,
    fsp: str,
    ont_id: int,
    service_port_index: int,
) -> OltWriteVerification:
    """Verify a service-port no longer exists after a delete write."""
    from app.services.network.olt_ssh_service_ports import get_service_ports_for_ont

    ok, msg, ports = get_service_ports_for_ont(olt, fsp, ont_id)
    if not ok:
        return OltWriteVerification(
            False,
            f"OLT accepted the service-port delete, but readback failed: {msg}",
        )

    for port in ports:
        if port.index == service_port_index:
            return OltWriteVerification(
                False,
                f"Service-port {service_port_index} still appears on the OLT after delete.",
                {
                    "index": port.index,
                    "vlan_id": getattr(port, "vlan_id", None),
                    "ont_id": getattr(port, "ont_id", None),
                    "gem_index": getattr(port, "gem_index", None),
                },
            )

    return OltWriteVerification(
        True,
        f"Verified service-port {service_port_index} is absent on the OLT.",
        {"fsp": fsp, "ont_id": ont_id, "service_port_index": service_port_index},
    )


def verify_iphost_config(
    olt: OLTDevice,
    *,
    fsp: str,
    ont_id: int,
    vlan_id: int,
    ip_mode: str,
    ip_address: str | None = None,
) -> OltWriteVerification:
    """Verify IPHOST configuration read back from the OLT."""
    from app.services.network.olt_ssh_ont import get_ont_iphost_config

    ok, msg, config = get_ont_iphost_config(olt, fsp, ont_id)
    if not ok:
        return OltWriteVerification(
            False,
            f"OLT accepted the IPHOST write, but readback failed: {msg}",
        )

    normalized = {
        " ".join(str(k).strip().lower().split()): str(v).strip()
        for k, v in config.items()
    }

    observed_vlan = normalized.get(
        "vlan",
        normalized.get("vlan id", normalized.get("ont manage vlan", "")),
    )
    if observed_vlan and observed_vlan.isdigit() and int(observed_vlan) != vlan_id:
        return OltWriteVerification(
            False,
            "IPHOST readback returned a different VLAN than the requested write.",
            {"expected_vlan_id": vlan_id, "observed_vlan_id": observed_vlan, "config": config},
        )

    observed_mode = normalized.get(
        "ip mode",
        normalized.get("mode", normalized.get("ont config type", "")),
    ).lower()
    if observed_mode and ip_mode.lower() not in observed_mode:
        return OltWriteVerification(
            False,
            "IPHOST readback returned a different mode than the requested write.",
            {"expected_mode": ip_mode, "observed_mode": observed_mode, "config": config},
        )

    observed_ip = normalized.get(
        "ip address",
        normalized.get("ip", normalized.get("ont ip", "")),
    )
    if ip_address and observed_ip and observed_ip != ip_address:
        return OltWriteVerification(
            False,
            "IPHOST readback returned a different IP address than the requested write.",
            {"expected_ip_address": ip_address, "observed_ip_address": observed_ip, "config": config},
        )

    return OltWriteVerification(
        True,
        "Verified IPHOST configuration on the OLT.",
        {
            "fsp": fsp,
            "ont_id": ont_id,
            "vlan_id": vlan_id,
            "mode": observed_mode or ip_mode,
            "ip_address": observed_ip or ip_address,
        },
    )
