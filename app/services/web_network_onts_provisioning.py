"""Route-facing service helpers for ONT provisioning web actions."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv4Network, ip_address, ip_network

from sqlalchemy.orm import Session, load_only

from app.models.network import (
    IPAssignment,
    IpPool,
    IPv4Address,
    IPVersion,
    MgmtIpMode,
    OntAssignment,
    OnuMode,
    WanMode,
)
from app.schemas.provisioning import ServiceOrderUpdate
from app.services import network as network_service
from app.services import provisioning as provisioning_service
from app.services import web_network_onts as web_network_onts_service
from app.services.common import coerce_uuid
from app.services.credential_crypto import encrypt_credential
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.ont_provisioning.preflight import validate_prerequisites
from app.services.network.ont_provisioning.result import StepResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JsonActionResult:
    content: dict[str, object]
    status_code: int = 200


_ONT_RESERVATION_PREFIX = "Reserved for ONT "


def _reservation_note_for_ont(ont_id: str) -> str:
    return f"{_ONT_RESERVATION_PREFIX}{ont_id}"


def _reservation_owner(notes: str | None) -> str | None:
    text = str(notes or "").strip()
    if not text.startswith(_ONT_RESERVATION_PREFIX):
        return None
    return text[len(_ONT_RESERVATION_PREFIX) :].strip() or None


def _pool_networks(pool: IpPool) -> list[IPv4Network]:
    blocks = [
        block
        for block in getattr(pool, "blocks", []) or []
        if getattr(block, "is_active", False)
    ]
    cidrs = [str(block.cidr) for block in blocks] or [str(pool.cidr)]
    networks: list[IPv4Network] = []
    for cidr in cidrs:
        try:
            network = ip_network(cidr, strict=False)
        except ValueError:
            continue
        if network.version == 4:
            networks.append(network)
    return networks


def _pool_netmask(pool: IpPool) -> str:
    networks = _pool_networks(pool)
    return str(networks[0].netmask) if networks else "255.255.255.0"


def _existing_ipv4_state(
    db: Session,
    addresses: Iterable[str],
) -> dict[str, tuple[IPv4Address, IPAssignment | None]]:
    address_values = list(
        dict.fromkeys(str(address) for address in addresses if address)
    )
    if not address_values:
        return {}
    rows = (
        db.query(IPv4Address, IPAssignment)
        .options(
            load_only(
                IPv4Address.id,
                IPv4Address.address,
                IPv4Address.pool_id,
                IPv4Address.is_reserved,
                IPv4Address.notes,
            )
        )
        .outerjoin(
            IPAssignment,
            IPAssignment.ipv4_address_id == IPv4Address.id,
        )
        .filter(IPv4Address.address.in_(address_values))
        .all()
    )
    return {str(address.address): (address, assignment) for address, assignment in rows}


def _ipv4_row_is_available(
    row: tuple[IPv4Address, IPAssignment | None] | None,
    *,
    normalized_pool_id: str,
    owner: str | None,
) -> bool:
    if not row:
        return True
    address, assignment = row
    reservation_owner = _reservation_owner(getattr(address, "notes", None))
    belongs_to_pool = not address.pool_id or str(address.pool_id) == normalized_pool_id
    reservation_available = not bool(address.is_reserved) or bool(
        owner and reservation_owner == owner
    )
    return assignment is None and reservation_available and belongs_to_pool


def available_static_ipv4_choices(
    db: Session,
    *,
    pool_id: str | None,
    ont_id: str | None = None,
    selected_ip: str | None = None,
    limit: int = 100,
) -> dict[str, object]:
    """Return available IPv4 choices for ONT static WAN assignment."""
    normalized_pool_id = str(pool_id or "").strip()
    if not normalized_pool_id:
        return {
            "pool": None,
            "choices": [],
            "recommended_ip": None,
            "netmask": "255.255.255.0",
            "gateway": "",
            "dns": "",
            "message": "Select an IP pool to see available addresses.",
        }
    try:
        pool_uuid = coerce_uuid(normalized_pool_id)
    except (TypeError, ValueError):
        pool_uuid = None
    pool = db.get(IpPool, pool_uuid) if pool_uuid else None
    if not pool or not getattr(pool, "is_active", False):
        return {
            "pool": None,
            "choices": [],
            "recommended_ip": None,
            "netmask": "255.255.255.0",
            "gateway": "",
            "dns": "",
            "message": "Selected IP pool is not available.",
        }
    pool_version = getattr(pool.ip_version, "value", pool.ip_version)
    if pool_version != IPVersion.ipv4.value:
        return {
            "pool": pool,
            "choices": [],
            "recommended_ip": None,
            "netmask": "255.255.255.0",
            "gateway": pool.gateway or "",
            "dns": ", ".join(v for v in [pool.dns_primary, pool.dns_secondary] if v),
            "message": "Only IPv4 pools can be used for static ONT IPv4 selection.",
        }

    selected = str(selected_ip or "").strip()
    owner = str(ont_id or "").strip() or None
    choices: list[dict[str, object]] = []
    seen: set[str] = set()
    gateway = str(pool.gateway or "").strip()
    max_choices = max(1, limit)

    def append_choice(ip_text: str, *, is_selected: bool = False) -> None:
        seen.add(ip_text)
        choices.append(
            {
                "address": ip_text,
                "label": ip_text,
                "recommended": False,
                "selected": is_selected,
            }
        )

    if selected:
        selected_address = None
        try:
            parsed_selected = ip_address(selected)
        except ValueError:
            parsed_selected = None
        if parsed_selected and parsed_selected.version == 4:
            selected_address = str(parsed_selected)
        selected_in_pool = bool(
            selected_address
            and selected_address != gateway
            and any(parsed_selected in network for network in _pool_networks(pool))
        )
        if selected_in_pool and selected_address:
            selected_state = _existing_ipv4_state(db, [selected_address])
            if _ipv4_row_is_available(
                selected_state.get(selected_address),
                normalized_pool_id=normalized_pool_id,
                owner=owner,
            ):
                append_choice(selected_address, is_selected=True)

    for network in _pool_networks(pool):
        iterator = network if network.prefixlen >= 31 else network.hosts()
        batch: list[str] = []
        for ip in iterator:
            ip_text = str(ip)
            if ip_text in seen or ip_text == gateway:
                continue
            batch.append(ip_text)
            if len(batch) < 512:
                continue
            state = _existing_ipv4_state(db, batch)
            for candidate in batch:
                if _ipv4_row_is_available(
                    state.get(candidate),
                    normalized_pool_id=normalized_pool_id,
                    owner=owner,
                ):
                    append_choice(candidate)
                    if len(choices) >= max_choices:
                        break
            batch = []
            if len(choices) >= max_choices:
                break
        if len(choices) < max_choices and batch:
            state = _existing_ipv4_state(db, batch)
            for candidate in batch:
                if _ipv4_row_is_available(
                    state.get(candidate),
                    normalized_pool_id=normalized_pool_id,
                    owner=owner,
                ):
                    append_choice(candidate)
                    if len(choices) >= max_choices:
                        break
        if len(choices) >= max_choices:
            break

    if choices:
        recommended = (
            selected
            if any(c["address"] == selected for c in choices)
            else str(choices[0]["address"])
        )
        for choice in choices:
            choice["recommended"] = choice["address"] == recommended
            choice["selected"] = choice["address"] == recommended
    else:
        recommended = None

    return {
        "pool": pool,
        "choices": choices,
        "recommended_ip": recommended,
        "netmask": _pool_netmask(pool),
        "gateway": gateway,
        "dns": ", ".join(v for v in [pool.dns_primary, pool.dns_secondary] if v),
        "message": None if choices else "No available IPv4 addresses in this pool.",
    }


def _reserve_static_ipv4_for_ont(
    db: Session,
    *,
    ont_id: str,
    pool_id: str,
    requested_ip: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    try:
        pool_uuid = coerce_uuid(pool_id)
    except (TypeError, ValueError):
        pool_uuid = None
    pool = db.get(IpPool, pool_uuid) if pool_uuid else None
    if not pool:
        raise ValueError("Selected IP pool is not available.")

    selected = str(requested_ip or "").strip()
    if selected:
        try:
            parsed_selected = ip_address(selected)
        except ValueError as exc:
            raise ValueError("Selected static IP is invalid.") from exc
        selected = str(parsed_selected)
        gateway = str(pool.gateway or "").strip()
        if parsed_selected.version != 4 or selected == gateway:
            raise ValueError("Selected static IP is not available in this pool.")
        if not any(parsed_selected in network for network in _pool_networks(pool)):
            raise ValueError("Selected static IP is not available in this pool.")
        state = _existing_ipv4_state(db, [selected])
        if not _ipv4_row_is_available(
            state.get(selected),
            normalized_pool_id=str(pool.id),
            owner=ont_id,
        ):
            raise ValueError("Selected static IP is not available in this pool.")
        choices_state: dict[str, object] = {
            "netmask": _pool_netmask(pool),
            "gateway": gateway,
            "dns": ", ".join(v for v in [pool.dns_primary, pool.dns_secondary] if v),
        }
    else:
        choices_state = available_static_ipv4_choices(
            db,
            pool_id=pool_id,
            ont_id=ont_id,
            limit=1,
        )
        selected = str(choices_state.get("recommended_ip") or "").strip()
        if not selected:
            raise ValueError(
                str(choices_state.get("message") or "No available IPv4 address.")
            )

    note = _reservation_note_for_ont(ont_id)
    previous = (
        db.query(IPv4Address)
        .filter(IPv4Address.notes == note)
        .filter(IPv4Address.address != selected)
        .all()
    )
    for address in previous:
        if not getattr(address, "assignment", None):
            address.is_reserved = False
            address.notes = None

    record = db.query(IPv4Address).filter(IPv4Address.address == selected).first()
    if record is None:
        record = IPv4Address(
            address=selected,
            pool_id=pool.id,
            is_reserved=True,
            notes=note,
        )
        db.add(record)
    else:
        if record.pool_id and str(record.pool_id) != str(pool.id):
            raise ValueError("Selected static IP belongs to a different pool.")
        if getattr(record, "assignment", None):
            raise ValueError("Selected static IP is already assigned.")
        owner = _reservation_owner(record.notes)
        if record.is_reserved and owner and owner != ont_id:
            raise ValueError("Selected static IP is already reserved.")
        record.pool_id = pool.id
        record.is_reserved = True
        record.notes = note
    db.flush()
    return (
        selected,
        str(choices_state.get("netmask") or "255.255.255.0"),
        str(choices_state.get("gateway") or ""),
        str(choices_state.get("dns") or ""),
    )


def _ip_pool_scope_for_ont_error(
    db: Session,
    *,
    ont: object,
    pool_id: str | None,
    vlan_id: str | None,
) -> str | None:
    normalized_pool_id = str(pool_id or "").strip()
    if not normalized_pool_id:
        return None
    try:
        pool_uuid = coerce_uuid(normalized_pool_id)
    except (TypeError, ValueError):
        return "Selected IP pool is not valid."
    pool = db.get(IpPool, pool_uuid)
    if not pool or not getattr(pool, "is_active", False):
        return "Selected IP pool is not available."

    ont_olt_id = str(getattr(ont, "olt_device_id", "") or "")
    pool_olt_id = str(getattr(pool, "olt_device_id", "") or "")
    if pool_olt_id and ont_olt_id and pool_olt_id != ont_olt_id:
        return "Selected IP pool belongs to a different OLT."

    selected_vlan_id = str(vlan_id or "").strip()
    pool_vlan_id = str(getattr(pool, "vlan_id", "") or "")
    if pool_vlan_id and selected_vlan_id and pool_vlan_id != selected_vlan_id:
        return "Selected IP pool belongs to a different VLAN."
    if pool_vlan_id and not selected_vlan_id:
        return "Select the VLAN linked to the selected IP pool."
    return None


def _effective_tr069_profile_id(
    db: Session,
    *,
    ont_id: str,
) -> int | None:
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    resolved_tr069_profile, _resolved_tr069_profile_error = (
        web_network_onts_service.resolve_effective_tr069_profile_for_ont(db, ont)
    )
    return getattr(
        resolved_tr069_profile,
        "profile_id",
        None,
    )


def provisioning_preview_context(
    db: Session,
    *,
    ont_id: str,
) -> dict[str, object]:
    """Return command-preview context for desired-config provisioning."""
    effective_tr069_profile_id = _effective_tr069_profile_id(
        db,
        ont_id=ont_id,
    )
    return {
        "ont_id": ont_id,
        "tr069_olt_profile_id": effective_tr069_profile_id,
        "message": "Command preview is generated from OntUnit.desired_config during direct provisioning.",
    }


def preflight_result(
    db: Session,
    *,
    ont_id: str,
) -> dict[str, object]:
    """Run provisioning preflight using OLT defaults and desired_config."""
    return validate_prerequisites(
        db,
        ont_id,
    )


def save_provision_settings(
    db: Session,
    *,
    ont_id: str,
    onu_mode: str | None,
    mgmt_ip_mode: str | None,
    mgmt_ip_address: str | None,
    mgmt_subnet: str | None,
    mgmt_gateway: str | None,
    wan_protocol: str | None,
    ip_pool_id: str | None,
    static_ip_pool_id: str | None,
    static_ip: str | None,
    static_subnet: str | None,
    static_gateway: str | None,
    static_dns: str | None,
    lan_ip: str | None,
    lan_subnet: str | None,
    dhcp_enabled: str | None,
    dhcp_start: str | None,
    dhcp_end: str | None,
    wifi_enabled: str | None,
    wifi_ssid: str | None,
    wifi_password: str | None,
    wifi_security_mode: str | None,
    wifi_channel: str | None,
    pppoe_username: str | None,
    pppoe_password: str | None,
) -> JsonActionResult:
    """Persist provision-page WAN settings without starting provisioning."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except Exception:
        return JsonActionResult(
            status_code=404,
            content={"success": False, "message": "ONT not found"},
        )

    onu_mode_value = (onu_mode or "").strip().lower() or None
    mgmt_ip_mode_value = (mgmt_ip_mode or "").strip().lower() or None
    mgmt_ip_address_value = (mgmt_ip_address or "").strip() or None
    mgmt_subnet_value = (mgmt_subnet or "").strip() or None
    mgmt_gateway_value = (mgmt_gateway or "").strip() or None
    wan_protocol_value = (wan_protocol or "").strip().lower() or None
    pppoe_username_value = (pppoe_username or "").strip() or None
    pppoe_password_value = (pppoe_password or "").strip() or None
    ip_pool_id_value = (ip_pool_id or "").strip() or None
    static_ip_pool_id_value = (static_ip_pool_id or "").strip() or None
    static_ip_value = (static_ip or "").strip() or None
    static_subnet_value = (static_subnet or "").strip() or None
    static_gateway_value = (static_gateway or "").strip() or None
    static_dns_value = (static_dns or "").strip() or None
    lan_ip_value = (lan_ip or "").strip() or None
    lan_subnet_value = (lan_subnet or "").strip() or None
    dhcp_enabled_value = _bool_from_form(dhcp_enabled)
    dhcp_start_value = (dhcp_start or "").strip() or None
    dhcp_end_value = (dhcp_end or "").strip() or None
    wifi_enabled_value = _bool_from_form(wifi_enabled)

    wifi_ssid_value = (wifi_ssid or "").strip() or None
    wifi_password_value = (wifi_password or "").strip() or None
    wifi_security_mode_value = (wifi_security_mode or "").strip() or None
    wifi_channel_value = (wifi_channel or "").strip() or None

    effective = resolve_effective_ont_config(db, ont)
    config_pack = effective.get("config_pack") if isinstance(effective, dict) else None
    effective_values = (
        effective.get("values", {}) if isinstance(effective, dict) else {}
    )
    internet_vlan = getattr(config_pack, "internet_vlan", None)
    internet_vlan_id = (
        str(getattr(internet_vlan, "id", "") or "").strip() or None
    )

    if wan_protocol_value == "static" and static_ip_pool_id_value:
        scope_error = _ip_pool_scope_for_ont_error(
            db,
            ont=ont,
            pool_id=static_ip_pool_id_value,
            vlan_id=internet_vlan_id,
        )
        if scope_error:
            return JsonActionResult(
                status_code=422,
                content={"success": False, "message": scope_error},
            )
        choices_state = available_static_ipv4_choices(
            db,
            pool_id=static_ip_pool_id_value,
            ont_id=ont_id,
            selected_ip=static_ip_value,
            limit=1 if not static_ip_value else 5000,
        )
        static_ip_value = (
            static_ip_value
            or str(choices_state.get("recommended_ip") or "").strip()
            or None
        )
        static_subnet_value = (
            static_subnet_value
            or str(choices_state.get("netmask") or "").strip()
            or None
        )
        static_gateway_value = (
            static_gateway_value
            or str(choices_state.get("gateway") or "").strip()
            or None
        )
        static_dns_value = (
            static_dns_value or str(choices_state.get("dns") or "").strip() or None
        )

    onu_mode_value = (
        onu_mode_value
        or str(effective_values.get("onu_mode") or "").strip()
    )
    mgmt_ip_mode_value = (
        mgmt_ip_mode_value
        or str(effective_values.get("mgmt_ip_mode") or "").strip()
    )
    mgmt_ip_address_value = mgmt_ip_address_value or str(
        effective_values.get("mgmt_ip_address") or ""
    ).strip() or None
    wan_protocol_value = (
        wan_protocol_value
        or str(effective_values.get("wan_mode") or "").strip()
    )
    pppoe_username_value = (
        pppoe_username_value
        or str(effective_values.get("pppoe_username") or "").strip()
        or None
    )
    lan_ip_value = lan_ip_value or str(effective_values.get("lan_ip") or "").strip() or None
    lan_subnet_value = (
        lan_subnet_value or str(effective_values.get("lan_subnet") or "").strip() or None
    )
    if dhcp_enabled_value is None:
        resolved_dhcp_enabled = effective_values.get("lan_dhcp_enabled")
        dhcp_enabled_value = (
            resolved_dhcp_enabled if isinstance(resolved_dhcp_enabled, bool) else None
        )
    dhcp_start_value = (
        dhcp_start_value
        or str(effective_values.get("lan_dhcp_start") or "").strip()
        or None
    )
    dhcp_end_value = (
        dhcp_end_value
        or str(effective_values.get("lan_dhcp_end") or "").strip()
        or None
    )
    if wifi_enabled_value is None:
        wifi_enabled_value = (
            effective_values.get("wifi_enabled")
            if effective_values.get("wifi_enabled") is not None
            else None
        )
    wifi_ssid_value = (
        wifi_ssid_value
        or str(effective_values.get("wifi_ssid") or "").strip()
    )
    wifi_security_mode_value = (
        wifi_security_mode_value
        or str(effective_values.get("wifi_security_mode") or "").strip()
    )
    wifi_channel_value = (
        wifi_channel_value
        or str(effective_values.get("wifi_channel") or "").strip()
    )

    field_issues = validate_provision_form_fields(
        onu_mode=onu_mode_value,
        mgmt_ip_mode=mgmt_ip_mode_value,
        mgmt_ip_address=mgmt_ip_address_value,
        mgmt_subnet=mgmt_subnet_value,
        mgmt_gateway=mgmt_gateway_value,
        wan_protocol=wan_protocol_value,
        pppoe_username=pppoe_username_value,
        static_ip_pool_id=static_ip_pool_id_value,
        static_ip=static_ip_value,
        static_subnet=static_subnet_value,
        static_gateway=static_gateway_value,
        static_dns=static_dns_value,
        lan_ip=lan_ip_value,
        lan_subnet=lan_subnet_value,
        dhcp_enabled=dhcp_enabled_value,
        dhcp_start=dhcp_start_value,
        dhcp_end=dhcp_end_value,
        wifi_enabled=wifi_enabled_value,
        wifi_ssid=wifi_ssid_value,
        wifi_password=wifi_password_value,
    )
    if field_issues:
        return JsonActionResult(
            status_code=422,
            content={
                "success": False,
                "message": "Provisioning configuration is incomplete: "
                + "; ".join(field_issues),
                "issues": field_issues,
            },
        )

    if onu_mode_value not in {None, OnuMode.routing.value, OnuMode.bridging.value}:
        return JsonActionResult(
            status_code=422,
            content={"success": False, "message": "Invalid ONU mode"},
        )

    if mgmt_ip_mode_value == "static":
        mgmt_ip_mode_value = "static_ip"
    if mgmt_ip_mode_value not in {None, "inactive", "dhcp", "static_ip"}:
        return JsonActionResult(
            status_code=422,
            content={"success": False, "message": "Invalid management IP mode"},
        )
    if mgmt_ip_mode_value == "static_ip" and not (
        mgmt_ip_address_value and mgmt_subnet_value and mgmt_gateway_value
    ):
        return JsonActionResult(
            status_code=422,
            content={
                "success": False,
                "message": "Static management IP requires address, subnet, and gateway",
            },
        )

    wan_mode_value: str | None = None
    if onu_mode_value == OnuMode.bridging.value:
        wan_mode_value = "bridge"
    elif wan_protocol_value == "pppoe":
        wan_mode_value = WanMode.pppoe.value
    elif wan_protocol_value == "dhcp":
        wan_mode_value = WanMode.dhcp.value
    elif wan_protocol_value == "static":
        wan_mode_value = WanMode.static_ip.value
    elif wan_protocol_value:
        return JsonActionResult(
            status_code=422,
            content={"success": False, "message": "Invalid WAN protocol"},
        )
    if wan_protocol_value == "static" and not (
        static_ip_value or static_ip_pool_id_value
    ):
        return JsonActionResult(
            status_code=422,
            content={
                "success": False,
                "message": "Static internet deployment requires an IP address or pool",
            },
        )
    if wan_protocol_value == "static" and static_ip_pool_id_value:
        scope_error = _ip_pool_scope_for_ont_error(
            db,
            ont=ont,
            pool_id=static_ip_pool_id_value,
            vlan_id=internet_vlan_id,
        )
        if scope_error:
            return JsonActionResult(
                status_code=422,
                content={"success": False, "message": scope_error},
            )
        try:
            (
                static_ip_value,
                reserved_subnet,
                reserved_gateway,
                reserved_dns,
            ) = _reserve_static_ipv4_for_ont(
                db,
                ont_id=ont_id,
                pool_id=static_ip_pool_id_value,
                requested_ip=static_ip_value,
            )
        except ValueError as exc:
            return JsonActionResult(
                status_code=422,
                content={"success": False, "message": str(exc)},
            )
        except Exception:
            db.rollback()
            logger.exception("Failed to reserve static IP for ONT %s", ont_id)
            return JsonActionResult(
                status_code=500,
                content={
                    "success": False,
                    "message": "Unable to reserve static IP. Please try again.",
                },
            )
        static_subnet_value = static_subnet_value or reserved_subnet
        static_gateway_value = static_gateway_value or reserved_gateway
        static_dns_value = static_dns_value or reserved_dns

    try:
        assignment = (
            effective.get("assignment") if isinstance(effective, dict) else None
        )
        if assignment is None:
            assignment = OntAssignment(ont_unit_id=ont.id, active=True)
            db.add(assignment)

        assignment.wan_mode = (
            OnuMode.bridging
            if onu_mode_value == OnuMode.bridging.value
            else OnuMode.routing
        )
        assignment.ip_mode = (
            MgmtIpMode.static_ip
            if wan_protocol_value == "static"
            else MgmtIpMode.dhcp
        )
        assignment.static_ip = static_ip_value if wan_protocol_value == "static" else None
        assignment.static_subnet = (
            static_subnet_value if wan_protocol_value == "static" else None
        )
        assignment.static_gateway = (
            static_gateway_value if wan_protocol_value == "static" else None
        )
        assignment.static_dns = static_dns_value if wan_protocol_value == "static" else None
        assignment.pppoe_username = (
            pppoe_username_value if wan_protocol_value == "pppoe" else None
        )
        if wan_protocol_value != "pppoe":
            assignment.pppoe_password = None
        elif pppoe_password_value:
            assignment.pppoe_password = encrypt_credential(pppoe_password_value)
        assignment.mgmt_ip_mode = (
            MgmtIpMode.static_ip
            if mgmt_ip_mode_value == "static_ip"
            else MgmtIpMode.dhcp
            if mgmt_ip_mode_value == "dhcp"
            else MgmtIpMode.inactive
        )
        assignment.mgmt_ip_address = (
            mgmt_ip_address_value if mgmt_ip_mode_value == "static_ip" else None
        )
        assignment.mgmt_subnet = (
            mgmt_subnet_value if mgmt_ip_mode_value == "static_ip" else None
        )
        assignment.mgmt_gateway = (
            mgmt_gateway_value if mgmt_ip_mode_value == "static_ip" else None
        )
        assignment.lan_ip = lan_ip_value
        assignment.lan_subnet = lan_subnet_value
        assignment.lan_dhcp_enabled = dhcp_enabled_value
        assignment.lan_dhcp_start = dhcp_start_value
        assignment.lan_dhcp_end = dhcp_end_value
        assignment.wifi_enabled = wifi_enabled_value
        assignment.wifi_ssid = wifi_ssid_value
        if wifi_password_value:
            assignment.wifi_password = encrypt_credential(wifi_password_value)
        assignment.wifi_security_mode = wifi_security_mode_value
        assignment.wifi_channel = wifi_channel_value

        if mgmt_ip_mode_value:
            update_service_order_execution_context_for_ont(
                db,
                ont_id=ont_id,
                step_name="configure_management_ip",
                values={
                    "ip_mode": "static"
                    if mgmt_ip_mode_value == "static_ip"
                    else mgmt_ip_mode_value,
                    "ip_address": mgmt_ip_address_value,
                    "subnet": mgmt_subnet_value,
                    "gateway": mgmt_gateway_value,
                },
                commit=False,
            )
        if any(
            value is not None
            for value in [
                lan_ip_value,
                lan_subnet_value,
                dhcp_enabled_value,
                dhcp_start_value,
                dhcp_end_value,
            ]
        ):
            update_service_order_execution_context_for_ont(
                db,
                ont_id=ont_id,
                step_name="configure_lan_tr069",
                values={
                    "lan_ip": lan_ip_value,
                    "lan_subnet": lan_subnet_value,
                    "dhcp_enabled": dhcp_enabled_value,
                    "dhcp_start": dhcp_start_value,
                    "dhcp_end": dhcp_end_value,
                },
                commit=False,
            )
        if any(
            value is not None
            for value in [
                wifi_enabled_value,
                wifi_ssid_value,
                wifi_password_value,
                wifi_security_mode_value,
                wifi_channel_value,
            ]
        ):
            update_service_order_execution_context_for_ont(
                db,
                ont_id=ont_id,
                step_name="configure_wifi_tr069",
                values={
                    "enabled": wifi_enabled_value,
                    "ssid": wifi_ssid_value,
                    "password_set": bool(wifi_password_value),
                    "security_mode": wifi_security_mode_value,
                    "channel": wifi_channel_value,
                },
                commit=False,
            )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to save provision settings for ONT %s", ont_id)
        return JsonActionResult(
            status_code=500,
            content={
                "success": False,
                "message": "Unable to save provision settings. Please try again.",
            },
        )
    return JsonActionResult(
        content={"success": True, "message": "Provision settings saved"}
    )


def validate_provision_form_fields(
    *,
    onu_mode: str | None,
    mgmt_ip_mode: str | None,
    mgmt_ip_address: str | None,
    mgmt_subnet: str | None,
    mgmt_gateway: str | None,
    wan_protocol: str | None,
    pppoe_username: str | None,
    static_ip_pool_id: str | None,
    static_ip: str | None,
    static_subnet: str | None,
    static_gateway: str | None,
    static_dns: str | None,
    lan_ip: str | None,
    lan_subnet: str | None,
    dhcp_enabled: bool | None,
    dhcp_start: str | None,
    dhcp_end: str | None,
    wifi_enabled: bool | None,
    wifi_ssid: str | None,
    wifi_password: str | None,
) -> list[str]:
    """Validate operator-selected provisioning inputs before allowing proceed."""
    issues: list[str] = []
    onu_mode_value = (onu_mode or "").strip().lower()
    mgmt_ip_mode_value = (mgmt_ip_mode or "").strip().lower()
    wan_protocol_value = (wan_protocol or "").strip().lower()

    if onu_mode_value not in {OnuMode.routing.value, OnuMode.bridging.value}:
        issues.append("Select ONU mode")
    if mgmt_ip_mode_value not in {"dhcp", "static", "static_ip"}:
        issues.append("Select management IP method")
    if mgmt_ip_mode_value in {"static", "static_ip"}:
        _require_ip(issues, "Management IP", mgmt_ip_address)
        _require_ip(issues, "Management subnet", mgmt_subnet)
        _require_ip(issues, "Management gateway", mgmt_gateway)

    if onu_mode_value == OnuMode.bridging.value:
        if wan_protocol_value not in {"bridged", "bridge"}:
            issues.append("Bridge ONU mode requires bridged WAN protocol")
    elif wan_protocol_value not in {"pppoe", "dhcp", "static"}:
        issues.append("Select internet deployment method")

    if wan_protocol_value == "pppoe" and not pppoe_username:
        issues.append("Enter PPPoE username")
    if wan_protocol_value == "static":
        if not static_ip_pool_id:
            _require_ip(issues, "Static IP address", static_ip)
        _require_ip(issues, "Static subnet", static_subnet)
        _require_ip(issues, "Static gateway", static_gateway)
        if static_dns:
            for dns in [item.strip() for item in static_dns.split(",") if item.strip()]:
                _require_ip(issues, f"DNS server {dns}", dns)

    if onu_mode_value == OnuMode.routing.value:
        _require_ip(issues, "LAN gateway IP", lan_ip)
        _require_ip(issues, "LAN subnet", lan_subnet)
        if dhcp_enabled is True:
            _require_ip(issues, "DHCP start", dhcp_start)
            _require_ip(issues, "DHCP end", dhcp_end)

    if wifi_enabled is True:
        if not wifi_ssid:
            issues.append("Enter WiFi SSID")
        if wifi_password and len(wifi_password) < 8:
            issues.append("WiFi password must be at least 8 characters")

    return issues


def _require_ip(issues: list[str], label: str, value: str | None) -> None:
    if not value:
        issues.append(f"{label} is required")
        return
    try:
        ip_address(value)
    except ValueError:
        issues.append(f"{label} is invalid")


def _bool_from_form(value: str | None) -> bool | None:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    return raw in {"true", "1", "yes", "on", "enabled"}


def _enum_value(value: object) -> str | None:
    raw = getattr(value, "value", value)
    text = str(raw or "").strip()
    return text or None


def update_service_order_execution_context_for_ont(
    db: Session,
    *,
    ont_id: str,
    step_name: str,
    values: dict[str, object],
    commit: bool = True,
) -> None:
    """Persist operator-selected step inputs onto the linked service order."""
    service_order_id = provisioning_service.resolve_service_order_id_for_ont(db, ont_id)
    if not service_order_id:
        return
    order = provisioning_service.service_orders.get(db, service_order_id)
    execution_context = dict(getattr(order, "execution_context", None) or {})
    ont_plan = dict(execution_context.get("ont_plan") or {})
    ont_plan[step_name] = {
        key: value for key, value in values.items() if value not in (None, "", [])
    }
    execution_context["ont_plan"] = ont_plan
    if commit:
        provisioning_service.service_orders.update(
            db,
            service_order_id,
            ServiceOrderUpdate(execution_context=execution_context),
        )
        return
    order.execution_context = execution_context


def record_ont_step_action(
    db: Session,
    *,
    ont_id: str,
    result: StepResult,
) -> None:
    """Record an operator-triggered ONT provisioning step."""
    logger.info(
        "ONT step %s for %s: success=%s waiting=%s - %s",
        result.step_name,
        ont_id,
        result.success,
        result.waiting,
        result.message,
    )
