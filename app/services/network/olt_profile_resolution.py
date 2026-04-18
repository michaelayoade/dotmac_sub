"""Resolve OLT authorization profiles from configured or live OLT data.

Huawei ONT authorization needs an ONT line profile and service profile. Those
IDs are OLT-local, so write workflows use OLT-scoped provisioning profile
records as their prerequisite. Live inventory helpers remain available for
operator sync/review flows, not as an implicit write-time fallback.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntProvisioningProfile, OnuType
from app.services.network.olt_ssh import OltProfileEntry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OntCapabilityCounts:
    """Port counts reported by an ONT or modeled in the local ONU catalog."""

    ethernet_ports: int | None = None
    voip_ports: int | None = None
    catv_ports: int | None = None

    @property
    def has_any_count(self) -> bool:
        return any(
            value is not None
            for value in (self.ethernet_ports, self.voip_ports, self.catv_ports)
        )


@dataclass(frozen=True)
class AuthorizationProfileResolution:
    """Resolved OLT-local profile IDs for ONT authorization."""

    line_profile_id: int
    service_profile_id: int
    message: str
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ServiceProfileDetail:
    """Parsed service profile detail used for capability matching."""

    profile_id: int
    name: str = ""
    ethernet_ports: int | None = None
    voip_ports: int | None = None
    catv_ports: int | None = None
    binding_count: int = 0


def _clean_model(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def _parse_enabled_value(value: str | None) -> bool:
    lowered = str(value or "").strip().lower()
    return lowered in {"enable", "enabled", "yes", "true", "on"}


def parse_line_profile_tr069_enabled(output: str) -> bool:
    """Return True when a line profile detail enables TR-069 management."""
    for line in output.splitlines():
        if "tr069" not in line.lower():
            continue
        key, sep, value = line.partition(":")
        if sep and "manage" in key.lower():
            return _parse_enabled_value(value)
        if re.search(r"\b(enable|enabled)\b", line, flags=re.IGNORECASE):
            return True
    return False


def parse_service_profile_detail(
    output: str,
    *,
    profile_id: int,
    name: str = "",
    binding_count: int = 0,
) -> ServiceProfileDetail:
    """Parse Huawei service profile detail enough to compare port counts."""

    def _match_count(pattern: str) -> int | None:
        match = re.search(pattern, output, flags=re.IGNORECASE)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    return ServiceProfileDetail(
        profile_id=profile_id,
        name=name,
        ethernet_ports=_match_count(
            r"\b(?:ETH|Ethernet)\s+(?:port\s+)?(?:number|num|count)\s*:\s*(\d+)"
        ),
        voip_ports=_match_count(
            r"\b(?:POTS|VOIP|VoIP)\s+(?:port\s+)?(?:number|num|count)\s*:\s*(\d+)"
        ),
        catv_ports=_match_count(
            r"\bCATV\s+(?:port\s+)?(?:number|num|count)\s*:\s*(\d+)"
        ),
        binding_count=binding_count,
    )


def parse_ont_capability_counts(output: str) -> OntCapabilityCounts:
    """Parse ONT capability output for ETH/POTS/CATV counts."""

    def _match_count(pattern: str) -> int | None:
        match = re.search(pattern, output, flags=re.IGNORECASE)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    return OntCapabilityCounts(
        ethernet_ports=_match_count(r"\b(?:ETH|Ethernet)\s+ports?\s*:\s*(\d+)"),
        voip_ports=_match_count(r"\b(?:POTS|VOIP|VoIP)\s+ports?\s*:\s*(\d+)"),
        catv_ports=_match_count(r"\bCATV\s+ports?\s*:\s*(\d+)"),
    )


def capability_from_onu_type(onu_type: OnuType | None) -> OntCapabilityCounts:
    """Build capability counts from the local ONU type catalog entry."""
    if onu_type is None:
        return OntCapabilityCounts()
    return OntCapabilityCounts(
        ethernet_ports=getattr(onu_type, "ethernet_ports", None),
        voip_ports=getattr(onu_type, "voip_ports", None),
        catv_ports=getattr(onu_type, "catv_ports", None),
    )


def choose_line_profile(
    profiles: list[OltProfileEntry],
    tr069_enabled_by_profile_id: dict[int, bool],
) -> OltProfileEntry | None:
    """Choose the best line profile from OLT data."""
    enabled = [
        profile
        for profile in profiles
        if tr069_enabled_by_profile_id.get(profile.profile_id) is True
    ]
    candidates = enabled or profiles
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda profile: (profile.binding_count, -profile.profile_id),
        reverse=True,
    )[0]


def choose_service_profile(
    profiles: list[ServiceProfileDetail],
    *,
    capability: OntCapabilityCounts,
    model: str | None = None,
) -> ServiceProfileDetail | None:
    """Choose the service profile that best fits the ONT capability/model."""
    if not profiles:
        return None

    requested = {
        "ethernet_ports": capability.ethernet_ports,
        "voip_ports": capability.voip_ports,
        "catv_ports": capability.catv_ports,
    }
    exact_matches: list[ServiceProfileDetail] = []
    if capability.has_any_count:
        for profile in profiles:
            matched = True
            for attr, expected in requested.items():
                observed = getattr(profile, attr)
                if expected is None or observed is None:
                    continue
                if observed != expected:
                    matched = False
                    break
            if matched:
                exact_matches.append(profile)
    if exact_matches:
        return sorted(
            exact_matches,
            key=lambda profile: (profile.binding_count, -profile.profile_id),
            reverse=True,
        )[0]

    clean_model = _clean_model(model)
    if clean_model:
        name_matches = [
            profile for profile in profiles if _clean_model(profile.name) == clean_model
        ]
        if name_matches:
            return sorted(
                name_matches,
                key=lambda profile: (profile.binding_count, -profile.profile_id),
                reverse=True,
            )[0]

    return sorted(
        profiles,
        key=lambda profile: (profile.binding_count, -profile.profile_id),
        reverse=True,
    )[0]


def _build_configured_resolution(
    profile: OntProvisioningProfile,
) -> tuple[bool, str, AuthorizationProfileResolution | None]:
    line_profile_id = profile.authorization_line_profile_id
    service_profile_id = profile.authorization_service_profile_id
    if line_profile_id is None or service_profile_id is None:
        return (
            False,
            (
                f"Provisioning profile '{profile.name}' is missing OLT authorization "
                "line/service profile IDs. Configure them before authorizing ONTs."
            ),
            None,
        )

    return (
        True,
        (
            f"Resolved OLT authorization profiles from provisioning profile "
            f"'{profile.name}': line {line_profile_id}, service {service_profile_id}."
        ),
        AuthorizationProfileResolution(
            line_profile_id=line_profile_id,
            service_profile_id=service_profile_id,
            message=(
                f"Resolved OLT authorization profiles from provisioning profile "
                f"'{profile.name}': line {line_profile_id}, service {service_profile_id}."
            ),
        ),
    )


def resolve_authorization_profiles_from_db(
    db: Session,
    olt: OLTDevice,
    *,
    profile: OntProvisioningProfile | None = None,
) -> tuple[bool, str, AuthorizationProfileResolution | None]:
    """Resolve OLT-local authorization profile IDs from stored DB config."""
    if profile is not None:
        if not profile.is_active:
            return (
                False,
                f"Provisioning profile '{profile.name}' is inactive.",
                None,
            )
        if profile.olt_device_id and profile.olt_device_id != olt.id:
            return (
                False,
                (
                    f"Provisioning profile '{profile.name}' belongs to another OLT. "
                    "Select an OLT-scoped profile for this device."
                ),
                None,
            )
        return _build_configured_resolution(profile)

    stmt = (
        select(OntProvisioningProfile)
        .where(
            OntProvisioningProfile.olt_device_id == olt.id,
            OntProvisioningProfile.is_active.is_(True),
        )
        .order_by(
            desc(OntProvisioningProfile.is_default),
            desc(OntProvisioningProfile.updated_at),
            desc(OntProvisioningProfile.created_at),
        )
    )
    configured_profile = db.scalars(stmt).first()
    if configured_profile is None:
        return (
            False,
            (
                f"No active provisioning profile is scoped to OLT '{olt.name}'. "
                "Create or select the OLT profile before authorizing ONTs."
            ),
            None,
        )

    return _build_configured_resolution(configured_profile)


def resolve_authorization_profiles(
    olt: OLTDevice,
    *,
    model: str | None = None,
    onu_type: OnuType | None = None,
) -> tuple[bool, str, AuthorizationProfileResolution | None]:
    """Resolve OLT-local line/service profile IDs for authorization."""
    from app.services.network import olt_ssh as core

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed while resolving OLT profiles: {exc}", None
    except Exception as exc:
        logger.error(
            "Error connecting to OLT %s for profile resolution: %s", olt.name, exc
        )
        return False, f"Unexpected profile resolution error: {type(exc).__name__}", None

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        core._run_huawei_cmd(channel, "screen-length 0 temporary")

        line_output = core._run_huawei_cmd(channel, "display ont-lineprofile gpon all")
        line_profiles = core._parse_profile_table(line_output)
        line_tr069: dict[int, bool] = {}
        for profile in line_profiles:
            detail = core._run_huawei_cmd(
                channel, f"display ont-lineprofile gpon profile-id {profile.profile_id}"
            )
            line_tr069[profile.profile_id] = parse_line_profile_tr069_enabled(detail)

        line_profile = choose_line_profile(line_profiles, line_tr069)
        if line_profile is None:
            return False, "No line profiles were found on the OLT.", None

        service_output = core._run_huawei_cmd(
            channel, "display ont-srvprofile gpon all"
        )
        service_entries = core._parse_profile_table(service_output)
        service_details: list[ServiceProfileDetail] = []
        for entry in service_entries:
            detail = core._run_huawei_cmd(
                channel, f"display ont-srvprofile gpon profile-id {entry.profile_id}"
            )
            service_details.append(
                parse_service_profile_detail(
                    detail,
                    profile_id=entry.profile_id,
                    name=entry.name,
                    binding_count=entry.binding_count,
                )
            )

        service_profile = choose_service_profile(
            service_details,
            capability=capability_from_onu_type(onu_type),
            model=model,
        )
        if service_profile is None:
            return False, "No service profiles were found on the OLT.", None

        warnings: list[str] = []
        if line_tr069.get(line_profile.profile_id) is not True:
            warnings.append(
                f"Selected line profile {line_profile.profile_id} because no TR-069-enabled line profile was identified."
            )

        resolution = AuthorizationProfileResolution(
            line_profile_id=line_profile.profile_id,
            service_profile_id=service_profile.profile_id,
            message=(
                f"Resolved OLT profiles from live inventory: line {line_profile.profile_id}, "
                f"service {service_profile.profile_id}."
            ),
            warnings=warnings,
        )
        logger.info(
            "Resolved authorization profiles for OLT %s model=%s line_profile_id=%d service_profile_id=%d warnings=%s",
            olt.name,
            model,
            resolution.line_profile_id,
            resolution.service_profile_id,
            warnings,
        )
        return True, resolution.message, resolution
    except Exception as exc:
        logger.error("Error resolving profiles on OLT %s: %s", olt.name, exc)
        return False, f"Error resolving OLT authorization profiles: {exc}", None
    finally:
        transport.close()


def _parse_match_state(output: str) -> str:
    match = re.search(r"\bMatch\s+state\s*:\s*([A-Za-z_-]+)", output, re.IGNORECASE)
    return match.group(1).strip().lower() if match else ""


def ensure_ont_service_profile_match(
    olt: OLTDevice,
    *,
    fsp: str,
    ont_id: int,
) -> tuple[bool, str]:
    """Fix service-profile mismatch after authorization using live capability."""
    from app.services.network import olt_ssh as core

    parts = fsp.split("/")
    if len(parts) != 3:
        return False, f"Invalid F/S/P format: {fsp!r}"
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed while checking profile match: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s for profile match: %s", olt.name, exc)
        return False, f"Unexpected profile match error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        core._run_huawei_cmd(channel, "screen-length 0 temporary")
        channel.send("config\n")
        core._read_until_prompt(channel, r"[#)]\s*$", timeout_sec=5)
        channel.send(f"interface gpon {frame_slot}\n")
        core._read_until_prompt(channel, r"[#)]\s*$", timeout_sec=5)

        info = core._run_huawei_cmd(channel, f"display ont info {port_num} {ont_id}")
        match_state = _parse_match_state(info)
        if match_state == "match":
            return True, "ONT service profile already matches live capability."

        capability_output = core._run_huawei_cmd(
            channel, f"display ont capability {port_num} {ont_id}"
        )
        capability = parse_ont_capability_counts(capability_output)
        if not capability.has_any_count:
            return (
                False,
                "ONT profile mismatch detected, but OLT capability output did not include usable port counts.",
            )

        service_output = core._run_huawei_cmd(
            channel, "display ont-srvprofile gpon all"
        )
        service_entries = core._parse_profile_table(service_output)
        service_details: list[ServiceProfileDetail] = []
        for entry in service_entries:
            detail = core._run_huawei_cmd(
                channel, f"display ont-srvprofile gpon profile-id {entry.profile_id}"
            )
            service_details.append(
                parse_service_profile_detail(
                    detail,
                    profile_id=entry.profile_id,
                    name=entry.name,
                    binding_count=entry.binding_count,
                )
            )

        service_profile = choose_service_profile(
            service_details,
            capability=capability,
        )
        if service_profile is None:
            return (
                False,
                "ONT profile mismatch detected, but no matching service profile was found.",
            )

        output = core._run_huawei_cmd(
            channel,
            f"ont modify {port_num} {ont_id} ont-srvprofile-id {service_profile.profile_id}",
            prompt=r"[#)]\s*$",
        )
        if core.is_error_output(output):
            return False, f"Failed to update service profile: {output.strip()[-200:]}"

        logger.info(
            "Updated ONT service profile for OLT %s %s ONT %d to profile %d after match_state=%s capability=%s",
            olt.name,
            fsp,
            ont_id,
            service_profile.profile_id,
            match_state or "unknown",
            capability,
        )
        return (
            True,
            f"Updated ONT service profile to {service_profile.profile_id} based on live capability.",
        )
    except Exception as exc:
        logger.error(
            "Error reconciling ONT service profile on OLT %s: %s", olt.name, exc
        )
        return False, f"Error reconciling ONT service profile: {exc}"
    finally:
        transport.close()
