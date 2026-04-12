"""Reconcile ONT provisioning profiles from live OLT service layout."""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.network import (
    ConfigMethod,
    IpProtocol,
    MgmtIpMode,
    OLTDevice,
    OntProfileType,
    OntProfileWanService,
    OntProvisioningProfile,
    OntUnit,
    OnuMode,
    PonPort,
    VlanMode,
    WanConnectionType,
    WanServiceType,
)
from app.services.network.olt_ssh import ServicePortEntry
from app.services.network.ont_provisioning.context import resolve_olt_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ObservedWanService:
    """A service inferred from live OLT data."""

    service_type: WanServiceType
    name: str
    vlan_id: int
    gem_port_id: int
    connection_type: WanConnectionType
    priority: int
    cos_priority: int | None = None


@dataclass(frozen=True)
class ObservedServicePort:
    """Service-port entry with its F/S/P retained from full OLT output."""

    fsp: str
    entry: ServicePortEntry


@dataclass(frozen=True)
class ObservedOltProvisioningProfile:
    """Live provisioning shape inferred from an OLT."""

    olt_id: str
    olt_name: str
    mgmt_vlan_tag: int
    mgmt_priority: int | None
    mgmt_config_mode: str | None
    services: list[ObservedWanService]
    sampled_onts: int
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OltProfileReconcileResult:
    """Result of reconciling one OLT provisioning profile."""

    success: bool
    message: str
    olt_name: str
    profile_name: str | None = None
    observed: ObservedOltProvisioningProfile | None = None
    changed: bool = False
    warnings: list[str] = field(default_factory=list)


def _int_value(value: object) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _choose_most_common(counter: Counter[int]) -> int | None:
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def _choose_service(counter: Counter[tuple[int, int]]) -> tuple[int, int] | None:
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def infer_profile_from_samples(
    *,
    olt: OLTDevice,
    service_port_samples: list[list[ServicePortEntry]],
    iphost_samples: list[dict[str, object]],
) -> ObservedOltProvisioningProfile | None:
    """Infer a profile from sampled service-port and IPHOST readbacks."""
    warnings: list[str] = []
    mgmt_vlan_counts: Counter[int] = Counter()
    mgmt_priority_counts: Counter[int] = Counter()
    mgmt_mode_counts: Counter[str] = Counter()
    for sample in iphost_samples:
        vlan = _int_value(
            sample.get("vlan")
            or sample.get("ONT manage VLAN")
            or sample.get("ont manage vlan")
        )
        priority = _int_value(
            sample.get("priority")
            or sample.get("ONT manage priority")
            or sample.get("ont manage priority")
        )
        mode = str(sample.get("mode") or sample.get("ONT config type") or "").strip()
        if vlan is not None:
            mgmt_vlan_counts[vlan] += 1
        if priority is not None:
            mgmt_priority_counts[priority] += 1
        if mode:
            mgmt_mode_counts[mode] += 1

    mgmt_vlan = _choose_most_common(mgmt_vlan_counts)
    if mgmt_vlan is None:
        return None

    all_service_counts: Counter[tuple[int, int]] = Counter()
    mgmt_service_counts: Counter[tuple[int, int]] = Counter()
    for ports in service_port_samples:
        for port in ports:
            key = (port.vlan_id, port.gem_index)
            all_service_counts[key] += 1
            if port.vlan_id == mgmt_vlan:
                mgmt_service_counts[key] += 1

    mgmt_service = _choose_service(mgmt_service_counts)
    if mgmt_service is None:
        warnings.append(
            f"IPHOST management VLAN {mgmt_vlan} was observed, but no matching service-port was sampled."
        )
        return None

    internet_counts = Counter(
        {
            key: count
            for key, count in all_service_counts.items()
            if key[0] != mgmt_vlan
        }
    )
    internet_service = _choose_service(internet_counts)
    if internet_service is None:
        warnings.append("No non-management service-port was sampled for internet.")
        return None

    mgmt_priority = _choose_most_common(mgmt_priority_counts)
    mgmt_config_mode = (
        mgmt_mode_counts.most_common(1)[0][0] if mgmt_mode_counts else None
    )
    if mgmt_config_mode and "static" in mgmt_config_mode.lower():
        warnings.append(
            "Sampled ONTs use static IPHOST addresses; the reusable profile keeps "
            "DHCP because static management IPs must come from per-ONT allocation."
        )
    services = [
        ObservedWanService(
            service_type=WanServiceType.internet,
            name="Internet PPPoE",
            vlan_id=internet_service[0],
            gem_port_id=internet_service[1],
            connection_type=WanConnectionType.pppoe,
            priority=1,
        ),
        ObservedWanService(
            service_type=WanServiceType.management,
            name="TR-069 Management",
            vlan_id=mgmt_service[0],
            gem_port_id=mgmt_service[1],
            connection_type=WanConnectionType.dhcp,
            priority=2,
            cos_priority=mgmt_priority,
        ),
    ]
    return ObservedOltProvisioningProfile(
        olt_id=str(olt.id),
        olt_name=olt.name,
        mgmt_vlan_tag=mgmt_vlan,
        mgmt_priority=mgmt_priority,
        mgmt_config_mode=mgmt_config_mode,
        services=services,
        sampled_onts=len(service_port_samples),
        warnings=warnings,
    )


def _site_name(olt: OLTDevice) -> str:
    return (olt.name or "OLT").replace(" Huawei OLT", "").strip() or olt.name


def _normalize_fsp(value: str | None) -> str | None:
    raw = str(value or "").strip()
    raw = re.sub(r"^(?:pon-|gpon\s+)", "", raw, flags=re.IGNORECASE).strip()
    return raw if re.fullmatch(r"\d+/\d+/\d+", raw) else None


def parse_service_port_observations(output: str) -> list[ObservedServicePort]:
    """Parse full Huawei service-port output while preserving F/S/P."""
    observations: list[ObservedServicePort] = []
    pattern = re.compile(
        r"^\s*(?P<index>\d+)\s+"
        r"(?P<vlan>\d+)\s+\S+\s+gpon\s+"
        r"(?P<frame_slot>\d+/\d+)\s*/(?P<port>\d+)\s+"
        r"(?P<ont>\d+)\s+(?P<gem>\d+)\s+"
        r"(?P<flow_type>\S+)\s+(?P<flow_para>\S+).*?\s+"
        r"(?P<state>up|down)\s*$",
        re.IGNORECASE,
    )
    for line in output.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        observations.append(
            ObservedServicePort(
                fsp=f"{match.group('frame_slot')}/{match.group('port')}",
                entry=ServicePortEntry(
                    index=int(match.group("index")),
                    vlan_id=int(match.group("vlan")),
                    ont_id=int(match.group("ont")),
                    gem_index=int(match.group("gem")),
                    flow_type=match.group("flow_type"),
                    flow_para=match.group("flow_para"),
                    state=match.group("state").lower(),
                ),
            )
        )
    return observations


def _get_or_create_scoped_profile(
    db: Session,
    olt: OLTDevice,
) -> OntProvisioningProfile:
    stmt = (
        select(OntProvisioningProfile)
        .options(selectinload(OntProvisioningProfile.wan_services))
        .where(
            OntProvisioningProfile.olt_device_id == olt.id,
            OntProvisioningProfile.is_active.is_(True),
        )
        .order_by(OntProvisioningProfile.name)
    )
    profile = db.scalars(stmt).first()
    if profile:
        return profile

    profile = OntProvisioningProfile(
        name=f"{_site_name(olt)} PPPoE",
        olt_device_id=olt.id,
        profile_type=OntProfileType.residential,
        config_method=ConfigMethod.tr069,
        onu_mode=OnuMode.routing,
        ip_protocol=IpProtocol.ipv4,
        mgmt_ip_mode=MgmtIpMode.dhcp,
        mgmt_remote_access=False,
        wifi_enabled=True,
        internet_config_ip_index=0,
        wan_config_profile_id=0,
        is_default=False,
        is_active=True,
    )
    db.add(profile)
    db.flush()
    return profile


def apply_observed_profile(
    db: Session,
    profile: OntProvisioningProfile,
    observed: ObservedOltProvisioningProfile,
) -> bool:
    """Apply observed OLT service layout to a scoped profile."""
    changed = False

    expected_name = f"{_site_name(profile.olt_device)} PPPoE mgmt{observed.mgmt_vlan_tag}"
    internet = next(
        (svc for svc in observed.services if svc.service_type == WanServiceType.internet),
        None,
    )
    if internet:
        expected_name += f" internet{internet.vlan_id}"
    profile_updates = {
        "name": expected_name,
        "mgmt_ip_mode": MgmtIpMode.dhcp,
        "mgmt_vlan_tag": observed.mgmt_vlan_tag,
        "config_method": ConfigMethod.tr069,
        "onu_mode": OnuMode.routing,
        "ip_protocol": IpProtocol.ipv4,
        "internet_config_ip_index": 0,
        "wan_config_profile_id": 0,
        "pppoe_omci_vlan": internet.vlan_id if internet else None,
    }
    for field_name, value in profile_updates.items():
        if getattr(profile, field_name) != value:
            setattr(profile, field_name, value)
            changed = True

    existing_by_type = {
        service.service_type: service
        for service in list(profile.wan_services)
        if service.service_type in {WanServiceType.internet, WanServiceType.management}
    }
    for observed_service in observed.services:
        service = existing_by_type.get(observed_service.service_type)
        if service is None:
            service = OntProfileWanService(
                profile_id=profile.id,
                service_type=observed_service.service_type,
            )
            db.add(service)
            changed = True
        service_updates = {
            "name": observed_service.name,
            "priority": observed_service.priority,
            "vlan_mode": VlanMode.tagged,
            "s_vlan": observed_service.vlan_id,
            "c_vlan": None,
            "cos_priority": observed_service.cos_priority,
            "connection_type": observed_service.connection_type,
            "ip_mode": None,
            "gem_port_id": observed_service.gem_port_id,
            "is_active": True,
        }
        for field_name, value in service_updates.items():
            if getattr(service, field_name) != value:
                setattr(service, field_name, value)
                changed = True

    db.flush()
    return changed


def reconcile_olt_profile_from_live(
    db: Session,
    olt_id: str,
    *,
    sample_limit: int = 8,
    dry_run: bool = False,
) -> OltProfileReconcileResult:
    """Read live OLT data and reconcile that OLT's scoped provisioning profile."""
    from app.services.network.olt_ssh import get_service_ports
    from app.services.network.olt_ssh_ont import get_ont_iphost_config
    from app.services.network.olt_ssh_service_ports import get_service_ports_for_ont

    olt = db.get(OLTDevice, olt_id)
    if not olt:
        return OltProfileReconcileResult(False, "OLT not found", olt_name="")

    effective_sample_limit = max(sample_limit, 8)
    ont_ids = [
        str(ont_id)
        for ont_id in db.scalars(
            select(OntUnit.id)
            .where(OntUnit.olt_device_id == olt.id)
            .order_by(OntUnit.updated_at.desc().nullslast(), OntUnit.created_at.desc())
            .limit(effective_sample_limit * 4)
        ).all()
    ]
    service_samples: list[list[ServicePortEntry]] = []
    iphost_samples: list[dict[str, object]] = []
    warnings: list[str] = []

    try:
        from app.services.network import olt_ssh as core

        transport, channel, _policy = core._open_shell(olt)
        try:
            channel.send("enable\n")
            core._read_until_prompt(channel, r"#\s*$", timeout_sec=10)
            output = core._run_huawei_paged_cmd(
                channel, "display service-port all", timeout_sec=60
            )
        finally:
            transport.close()

        by_ont: dict[tuple[str, int], list[ServicePortEntry]] = {}
        for observation in parse_service_port_observations(output):
            if observation.entry.state != "up":
                continue
            by_ont.setdefault((observation.fsp, observation.entry.ont_id), []).append(
                observation.entry
            )

        for (fsp, ont_id_on_olt), ont_ports in by_ont.items():
            if len(service_samples) >= effective_sample_limit:
                break
            if len(ont_ports) < 2:
                continue
            iphost_ok, _iphost_msg, iphost = get_ont_iphost_config(
                olt, fsp, ont_id_on_olt
            )
            if not iphost_ok or not iphost:
                continue
            service_samples.append(ont_ports)
            iphost_samples.append(dict(iphost))
    except Exception as exc:
        warnings.append(f"Full service-port scan failed: {exc}")

    pon_names: list[str] = []
    for pon_name in db.scalars(
        select(PonPort.name)
        .where(PonPort.olt_id == olt.id, PonPort.is_active.is_(True))
        .order_by(PonPort.name)
    ).all():
        normalized_fsp = _normalize_fsp(pon_name)
        if normalized_fsp:
            pon_names.append(normalized_fsp)
    for fsp in pon_names:
        if len(service_samples) >= effective_sample_limit:
            break
        ok, msg, ports = get_service_ports(olt, fsp)
        if not ok or not ports:
            warnings.append(f"{fsp}: {msg}")
            continue
        ports_by_ont: dict[int, list[ServicePortEntry]] = {}
        for port in ports:
            ports_by_ont.setdefault(port.ont_id, []).append(port)
        for ont_id_on_olt, ont_ports in ports_by_ont.items():
            if len(service_samples) >= effective_sample_limit:
                break
            if len(ont_ports) < 2:
                continue
            iphost_ok, _iphost_msg, iphost = get_ont_iphost_config(
                olt, fsp, ont_id_on_olt
            )
            if not iphost_ok or not iphost:
                continue
            service_samples.append(ont_ports)
            iphost_samples.append(dict(iphost))

    for ont_id in ont_ids:
        if len(service_samples) >= effective_sample_limit:
            break
        ctx, err = resolve_olt_context(db, ont_id)
        if not ctx:
            warnings.append(err or f"Could not resolve OLT context for ONT {ont_id}")
            continue
        logger.info(
            "Sampling OLT %s ONT %s at %s/%s",
            olt.name,
            ctx.ont.serial_number,
            ctx.fsp,
            ctx.olt_ont_id,
        )
        ok, msg, ports = get_service_ports_for_ont(ctx.olt, ctx.fsp, ctx.olt_ont_id)
        if not ok or not ports:
            warnings.append(msg)
            continue
        iphost_ok, _iphost_msg, iphost = get_ont_iphost_config(
            ctx.olt, ctx.fsp, ctx.olt_ont_id
        )
        if not iphost_ok or not iphost:
            warnings.append(
                f"No IPHOST readback for ONT {ctx.ont.serial_number} on {ctx.fsp}/{ctx.olt_ont_id}"
            )
            continue
        service_samples.append(ports)
        iphost_samples.append(dict(iphost))

    observed = infer_profile_from_samples(
        olt=olt,
        service_port_samples=service_samples,
        iphost_samples=iphost_samples,
    )
    if observed is None:
        return OltProfileReconcileResult(
            False,
            "Could not infer management and internet services from live OLT samples.",
            olt_name=olt.name,
            warnings=warnings,
        )

    profile = _get_or_create_scoped_profile(db, olt)
    changed = False
    if not dry_run:
        changed = apply_observed_profile(db, profile, observed)
        db.commit()
        db.refresh(profile)

    return OltProfileReconcileResult(
        True,
        f"Reconciled from {observed.sampled_onts} sampled ONT(s).",
        olt_name=olt.name,
        profile_name=profile.name,
        observed=observed,
        changed=changed,
        warnings=[*warnings, *observed.warnings],
    )


def reconcile_all_olt_profiles_from_live(
    db: Session,
    *,
    sample_limit: int = 8,
    dry_run: bool = False,
) -> list[OltProfileReconcileResult]:
    """Reconcile all active OLT scoped profiles from live OLT data."""
    olts = db.scalars(
        select(OLTDevice).where(OLTDevice.is_active.is_(True)).order_by(OLTDevice.name)
    ).all()
    return [
        reconcile_olt_profile_from_live(
            db,
            str(olt.id),
            sample_limit=sample_limit,
            dry_run=dry_run,
        )
        for olt in olts
    ]
