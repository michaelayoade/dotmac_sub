"""DB ↔ in-memory dataclass adapters for the reconciler.

Bridges the typed ``OntDesiredState`` / ``OntObservedState`` dataclasses (see
``state.py``) and the existing SQLAlchemy ORM (``OntUnit``, ``OntObservation``,
plus the resolved-effective-config helper in
``app.services.network.effective_ont_config``).

Four functions:

* ``desired_from_ont_unit(db, ont)`` — read current desired state.
* ``apply_proposed_change(ont, target)`` — write a successful reconcile's
  desired-state mutation back to ``OntUnit`` (structural columns + the
  ``desired_config`` JSON blob accessor properties already on the model).
* ``observed_from_ont_observation(obs)`` — read the 1:1 observation row.
* ``upsert_ont_observation(db, ont_unit_id, observed)`` — persist a fresh
  ``OntObservedState`` (INSERT or UPDATE).

Field provenance — most config knobs come from the existing
``resolve_effective_ont_config`` (which already composes ``OntUnit.desired_config``
with ``OltConfigPack`` defaults and per-OLT-equipment profile mappings).
Identity, reconciler bookkeeping, and ACS-server FK come from typed columns on
``OntUnit`` directly. A handful of fields are not yet sourced from the existing
model and use sensible defaults — they're marked with ``DEFAULT:`` comments
and will be filled in by follow-up commits when their sources land.

This module is read by the reader/planner/applier code in subsequent commits;
no production code path calls it yet.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.models.ont_observation import OntObservation
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.serial_utils import parse_ont_id_on_olt

from .state import (
    AcsObservedFields,
    OltObservedFields,
    OntDesiredState,
    OntObservedState,
)

_FSP_RE = re.compile(r"^\d+/\d+/\d+$")


# ── Desired state ───────────────────────────────────────────────────────────


def desired_from_ont_unit(db: Session, ont: OntUnit) -> OntDesiredState:
    """Materialise the current ``OntDesiredState`` for one ONT.

    The bulk of the configuration comes from
    ``resolve_effective_ont_config(db, ont)``, which composes
    ``OntUnit.desired_config`` (the per-ONT JSON blob with accessor properties)
    with ``OltConfigPack`` defaults and the per-OLT-equipment
    ``OltOnuTypeProfileMapping``. Identity and reconciler-bookkeeping fields
    come from typed columns on ``OntUnit``.

    A few fields have ``DEFAULT:`` placeholder values because their sources are
    not yet wired into the effective-config helper — those land in follow-up
    commits as the readers / planner need them.
    """
    effective = resolve_effective_ont_config(db, ont)
    values: dict[str, Any] = (
        effective.get("values", {}) if isinstance(effective, dict) else {}
    )

    fsp = _fsp_from_ont(ont)
    olt_ont_id = parse_ont_id_on_olt(ont.external_id) or 0
    # The effective-config helper exposes two related fields: ``wan_mode`` is
    # the IP-mode (``pppoe``/``bridge``/``static_ip``/...) and ``onu_mode`` is
    # the ONU operating mode (``routing``/``bridging``). Either being "bridge"
    # or "bridging" means the reconciler treats the WAN as bridged.
    wan_mode = _normalise_wan_mode(values.get("wan_mode"), values.get("onu_mode"))

    return OntDesiredState(
        ont_unit_id=str(ont.id),
        serial_number=ont.serial_number,
        olt_id=str(ont.olt_device_id) if ont.olt_device_id else "",
        fsp=fsp,
        olt_ont_id=int(olt_ont_id),
        line_profile_id=int(values.get("authorization_line_profile_id") or 0),
        service_profile_id=int(values.get("authorization_service_profile_id") or 0),
        # DEFAULT: description isn't computed by effective_ont_config; the
        # reconciler's initial value falls back to a serial-stamped stub
        # (the same one Fix #7 emits at ont add time). When subscriber
        # binding lands the reconciler will recompute on assignment changes.
        description=_default_description(ont),
        mgmt_vlan=_int_or_none(values.get("mgmt_vlan")),
        mgmt_ip=values.get("mgmt_ip_address"),
        mgmt_subnet_mask=values.get("mgmt_subnet"),
        mgmt_gateway=values.get("mgmt_gateway"),
        # DEFAULT: DNS isn't carried through effective_ont_config; fleet uses
        # 8.8.8.8 / 4.2.2.2 (per reference_ont_provisioning).
        mgmt_dns_primary="8.8.8.8",
        mgmt_dns_secondary="4.2.2.2",
        # DEFAULT: priority isn't resolved here; resolver lives in
        # iphost_priority.resolve_management_iphost_priority and is plan-time.
        mgmt_iphost_priority=2,
        tr069_profile_id=int(values.get("tr069_olt_profile_id") or 0),
        acs_server_id=str(ont.tr069_acs_server_id) if ont.tr069_acs_server_id else None,
        cr_username=values.get("cr_username"),
        cr_password_ref=values.get("cr_password"),
        # DEFAULT: interval not carried; fleet standard is 300s.
        periodic_inform_interval_sec=300,
        wan_mode=wan_mode,
        wan_vlan=_int_or_none(values.get("wan_vlan")),
        wan_gem_index=_int_or_none(values.get("wan_gem_index")),
        wan_pppoe_username=values.get("pppoe_username"),
        wan_pppoe_password_ref=values.get("pppoe_password"),
        # DEFAULT: provisioning method comes from a global domain_settings
        # row read elsewhere; until the reader pass loads it, default to "auto".
        wan_pppoe_provisioning_method="auto",
        wan_pppoe_wcd_index=int(values.get("pppoe_wcd_index") or 1),
        wan_pppoe_instance_index=int(values.get("wan_instance_index") or 1),
        wan_config_profile_id=_int_or_none(values.get("wan_config_profile_id")),
        wan_internet_config_ip_index=_int_or_none(
            values.get("internet_config_ip_index")
        ),
        # DEFAULT: nat/ipv6/dhcp explicit toggles aren't on every ONT today;
        # routed mode implies NAT+DHCP on (fleet convention from feedback_ont_setup_defaults).
        nat_enabled=wan_mode == "pppoe",
        ipv6_enabled=False,
        dhcp_enabled=_bool_or_default(values.get("lan_dhcp_enabled"), default=True),
        dhcp_pool_min=values.get("lan_dhcp_start") or "192.168.100.2",
        dhcp_pool_max=values.get("lan_dhcp_end") or "192.168.100.254",
        dhcp_subnet_mask=_subnet_mask_from_lan_subnet(values.get("lan_subnet")),
        wifi_ssid=values.get("wifi_ssid") or "",
        wifi_password_ref=values.get("wifi_password") or "",
        # DEFAULT: push tracking lands when the applier records WiFi pushes.
        wifi_password_pushed_at=None,
        # DEFAULT: service-port indices come from the allocator at first
        # provisioning; until that's wired into OntUnit, these are None
        # (which the planner treats as "needs allocation").
        mgmt_service_port_index=None,
        wan_service_port_index=None,
        # DEFAULT: forward-compat for Splynx → in-app Subscriber migration.
        subscriber_external_id=ont.external_id,
        wan_uprate_kbps=None,
        wan_downrate_kbps=None,
    )


def apply_proposed_change(ont: OntUnit, target: OntDesiredState) -> None:
    """Write a successful proposed_change back to ``OntUnit``.

    Identity fields are not mutated (validator rejects identity changes before
    this is called). Configuration fields route through the existing
    ``desired_config`` JSON sections via the model's accessor properties so the
    storage layout stays consistent with the rest of the codebase.

    This is a side-effecting write on the passed ``OntUnit`` instance — the
    caller is responsible for the surrounding ``db.commit()``.
    """
    # Configuration knobs that live in desired_config JSON sections
    ont.pppoe_username = target.wan_pppoe_username
    ont.pppoe_password = target.wan_pppoe_password_ref

    if hasattr(ont, "mgmt_ip_address"):
        ont.mgmt_ip_address = target.mgmt_ip

    _set_desired_value(ont, "wifi", "ssid", target.wifi_ssid)
    _set_desired_value(ont, "wifi", "password", target.wifi_password_ref)

    _set_desired_value(ont, "lan", "dhcp_enabled", target.dhcp_enabled)
    _set_desired_value(ont, "lan", "dhcp_start", target.dhcp_pool_min)
    _set_desired_value(ont, "lan", "dhcp_end", target.dhcp_pool_max)
    _set_desired_value(ont, "lan", "subnet", target.dhcp_subnet_mask)

    _set_desired_value(ont, "wan", "onu_mode", target.wan_mode)
    _set_desired_value(ont, "wan", "instance_index", target.wan_pppoe_instance_index)

    _set_desired_value(ont, "management", "subnet", target.mgmt_subnet_mask)
    _set_desired_value(ont, "management", "gateway", target.mgmt_gateway)


# ── Observed state ──────────────────────────────────────────────────────────


def observed_from_ont_observation(
    obs: OntObservation | None,
) -> OntObservedState | None:
    """Build an in-memory ``OntObservedState`` from a 1:1 observation row.

    Returns ``None`` for ONTs that have never been reconciled (no observation
    row exists yet).
    """
    if obs is None:
        return None
    return OntObservedState(
        last_reconciled_at=obs.last_reconciled_at,
        last_reconcile_duration_ms=obs.last_reconcile_duration_ms,
        mgmt_ip_pingable=obs.mgmt_ip_pingable,
        # consecutive_sweep_unreachable lives on OntUnit (the counter is read
        # by sync reconciles too, not just the sweeper); adapter callers fill
        # it from the ONT row when materialising the full observed state.
        consecutive_sweep_unreachable=0,
        olt=OltObservedFields(
            olt_present=obs.olt_present,
            olt_match_state=obs.olt_match_state,
            olt_run_state=obs.olt_run_state,
            olt_distance_m=obs.olt_distance_m,
            olt_rx_dbm=obs.olt_rx_dbm,
            olt_tx_dbm=obs.olt_tx_dbm,
            olt_temperature_c=obs.olt_temperature_c,
            olt_description=obs.olt_description,
            olt_mgmt_ip=obs.olt_mgmt_ip,
            olt_mgmt_vlan=obs.olt_mgmt_vlan,
            olt_line_profile_id=obs.olt_line_profile_id,
            olt_service_profile_id=obs.olt_service_profile_id,
            olt_service_ports=tuple(obs.olt_service_ports or ()),
        ),
        acs=AcsObservedFields(
            acs_present=obs.acs_present,
            acs_last_inform_at=obs.acs_last_inform_at,
            acs_last_boot_at=obs.acs_last_boot_at,
            acs_last_bootstrap_at=obs.acs_last_bootstrap_at,
            acs_observed_software_version=obs.acs_observed_software_version,
            acs_observed_pppoe_username=obs.acs_observed_pppoe_username,
            acs_observed_pppoe_enable=obs.acs_observed_pppoe_enable,
            acs_observed_wan_vlan=obs.acs_observed_wan_vlan,
            acs_observed_wan_external_ip=obs.acs_observed_wan_external_ip,
            acs_observed_wan_connection_status=obs.acs_observed_wan_connection_status,
            acs_observed_nat_enabled=obs.acs_observed_nat_enabled,
            acs_observed_dhcp_enabled=obs.acs_observed_dhcp_enabled,
            acs_observed_ssid=obs.acs_observed_ssid,
            acs_observed_periodic_inform_interval_sec=(
                obs.acs_observed_periodic_inform_interval_sec
            ),
            acs_observed_cr_username_set=obs.acs_observed_cr_username_set,
            acs_observed_cr_password_set=obs.acs_observed_cr_password_set,
            acs_observed_wan_wcd_index=obs.acs_observed_wan_wcd_index,
            acs_observed_wan_instance_index=obs.acs_observed_wan_instance_index,
        ),
    )


def upsert_ont_observation(
    db: Session,
    ont_unit_id: uuid.UUID | str,
    observed: OntObservedState,
) -> OntObservation:
    """Insert or update the 1:1 observation row for an ONT.

    The caller is responsible for ``db.commit()``. Returns the persisted ORM
    instance so callers can inspect the assigned UUID / timestamps.
    """
    ont_uuid = _coerce_uuid(ont_unit_id)
    existing = db.scalars(
        select(OntObservation).where(OntObservation.ont_unit_id == ont_uuid)
    ).first()

    if existing is None:
        row = OntObservation(ont_unit_id=ont_uuid)
        db.add(row)
    else:
        row = existing

    row.last_reconciled_at = observed.last_reconciled_at
    row.last_reconcile_duration_ms = observed.last_reconcile_duration_ms
    row.mgmt_ip_pingable = observed.mgmt_ip_pingable
    row.olt_present = observed.olt.olt_present
    row.olt_match_state = observed.olt.olt_match_state
    row.olt_run_state = observed.olt.olt_run_state
    row.olt_distance_m = observed.olt.olt_distance_m
    row.olt_rx_dbm = observed.olt.olt_rx_dbm
    row.olt_tx_dbm = observed.olt.olt_tx_dbm
    row.olt_temperature_c = observed.olt.olt_temperature_c
    row.olt_description = observed.olt.olt_description
    row.olt_mgmt_ip = observed.olt.olt_mgmt_ip
    row.olt_mgmt_vlan = observed.olt.olt_mgmt_vlan
    row.olt_line_profile_id = observed.olt.olt_line_profile_id
    row.olt_service_profile_id = observed.olt.olt_service_profile_id
    row.olt_service_ports = list(observed.olt.olt_service_ports)

    row.acs_present = observed.acs.acs_present
    row.acs_last_inform_at = observed.acs.acs_last_inform_at
    row.acs_last_boot_at = observed.acs.acs_last_boot_at
    row.acs_last_bootstrap_at = observed.acs.acs_last_bootstrap_at
    row.acs_observed_software_version = observed.acs.acs_observed_software_version
    row.acs_observed_pppoe_username = observed.acs.acs_observed_pppoe_username
    row.acs_observed_pppoe_enable = observed.acs.acs_observed_pppoe_enable
    row.acs_observed_wan_vlan = observed.acs.acs_observed_wan_vlan
    row.acs_observed_wan_external_ip = observed.acs.acs_observed_wan_external_ip
    row.acs_observed_wan_connection_status = (
        observed.acs.acs_observed_wan_connection_status
    )
    row.acs_observed_nat_enabled = observed.acs.acs_observed_nat_enabled
    row.acs_observed_dhcp_enabled = observed.acs.acs_observed_dhcp_enabled
    row.acs_observed_ssid = observed.acs.acs_observed_ssid
    row.acs_observed_periodic_inform_interval_sec = (
        observed.acs.acs_observed_periodic_inform_interval_sec
    )
    row.acs_observed_cr_username_set = observed.acs.acs_observed_cr_username_set
    row.acs_observed_cr_password_set = observed.acs.acs_observed_cr_password_set
    row.acs_observed_wan_wcd_index = observed.acs.acs_observed_wan_wcd_index
    row.acs_observed_wan_instance_index = observed.acs.acs_observed_wan_instance_index

    db.flush()  # Establish row.id before the caller commits.
    return row


# ── Internal helpers ────────────────────────────────────────────────────────


def _fsp_from_ont(ont: OntUnit) -> str:
    """Build the canonical f/s/p string from ``OntUnit.board`` + ``OntUnit.port``.

    Matches ``_scanned_fsp_from_ont`` in ``ont_olt_context.py`` — duplicated
    here to keep ``adapters.py`` self-contained (no cross-module dependency on
    a write-side context resolver). Returns the empty string when board/port
    don't form a valid f/s/p.
    """
    board = str(getattr(ont, "board", "") or "").strip()
    port = str(getattr(ont, "port", "") or "").strip()
    if not board or not port:
        return ""
    fsp = f"{board}/{port}"
    return fsp if _FSP_RE.fullmatch(fsp) else ""


def _default_description(ont: OntUnit) -> str:
    """Stub description used when no explicit one is bound.

    Mirrors ``_build_initial_ont_description`` in ``ont_authorization.py`` —
    serial + auth date. Subscribed-aware descriptions are introduced when the
    in-app Subscriber model lands.
    """
    return f"{ont.serial_number}_authd_{datetime.now(UTC).strftime('%Y%m%d')}"


def _normalise_wan_mode(ip_mode: Any, onu_mode: Any) -> str:
    """Reduce the effective config's two related fields to the reconciler's
    single ``pppoe`` / ``bridge`` contract.

    ``ip_mode`` is the WAN IP mode (``pppoe``/``bridge``/``static_ip``/...).
    ``onu_mode`` is the ONU operating mode (``routing``/``bridging``).
    A bridge signal in either field wins.
    """
    bridge_signals = {"bridge", "bridged", "bridging", "setup_via_onu"}
    if str(ip_mode or "").strip().lower() in bridge_signals:
        return "bridge"
    if str(onu_mode or "").strip().lower() in bridge_signals:
        return "bridge"
    return "pppoe"


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_default(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return default


def _subnet_mask_from_lan_subnet(raw: Any) -> str:
    """Effective config emits ``lan_subnet`` as CIDR or plain mask. The
    reconciler stores a dotted-decimal subnet mask; normalize here so the
    validator's mask check sees a consistent shape. Defaults to /24 on
    unparseable input."""
    if raw is None:
        return "255.255.255.0"
    text = str(raw).strip()
    if "/" in text:
        try:
            import ipaddress

            return str(ipaddress.ip_network(text, strict=False).netmask)
        except ValueError:
            return "255.255.255.0"
    return text or "255.255.255.0"


def _set_desired_value(ont: OntUnit, section: str, key: str, value: Any) -> None:
    """Write into ``OntUnit.desired_config[section][key]`` via the model's
    accessor pattern. None/empty removes the key. Mirrors the private
    ``OntUnit._set_desired_value`` helper but called from outside the model so
    we don't depend on a private API."""
    config = dict(ont.desired_config or {})
    section_values = dict(config.get(section) or {})
    if value in (None, ""):
        section_values.pop(key, None)
    else:
        section_values[key] = value
    if section_values:
        config[section] = section_values
    else:
        config.pop(section, None)
    ont.desired_config = config


def _coerce_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
