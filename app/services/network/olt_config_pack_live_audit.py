"""Live OLT config-pack audit.

This module compares Dotmac's saved OLT config pack against read-only Huawei
OLT profile output. It is intended for OLT onboarding and periodic audits, not
per-ONT authorization.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import OLTDevice
from app.services.network.olt_config_pack import resolve_olt_config_pack
from app.services.network.olt_profile_resolution import parse_line_profile_tr069_enabled
from app.services.network.olt_ssh_profiles import _parse_tr069_profile_detail

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveLineProfileDetail:
    profile_id: int
    gem_indexes: set[int] = field(default_factory=set)
    tr069_management_enabled: bool = False
    tr069_ip_index: int | None = None


@dataclass(frozen=True)
class LiveTr069ProfileDetail:
    profile_id: int
    exists: bool
    name: str = ""
    acs_url: str = ""


@dataclass
class OltConfigPackLiveAudit:
    olt_id: str
    olt_name: str
    success: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    observed: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return self.success and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "olt_id": self.olt_id,
            "olt_name": self.olt_name,
            "success": self.success,
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "observed": self.observed,
        }


@dataclass(frozen=True)
class CompatibleLineProfileSuggestion:
    profile_id: int
    name: str
    gem_indexes: set[int]
    tr069_management_enabled: bool
    tr069_ip_index: int | None
    binding_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "gem_indexes": sorted(self.gem_indexes),
            "tr069_management_enabled": self.tr069_management_enabled,
            "tr069_ip_index": self.tr069_ip_index,
            "binding_count": self.binding_count,
        }


def parse_line_profile_detail(output: str, *, profile_id: int) -> LiveLineProfileDetail:
    """Parse Huawei line-profile detail for GEM/TR-069 compatibility checks."""
    gem_indexes: set[int] = set()
    for match in re.finditer(r"<\s*Gem\s+Index\s+(\d+)\s*>", output, re.IGNORECASE):
        gem_indexes.add(int(match.group(1)))

    tr069_ip_index: int | None = None
    ip_index_match = re.search(r"\bTR069\s+IP\s+index\s*:\s*(\d+)", output, re.I)
    if ip_index_match:
        tr069_ip_index = int(ip_index_match.group(1))

    return LiveLineProfileDetail(
        profile_id=profile_id,
        gem_indexes=gem_indexes,
        tr069_management_enabled=parse_line_profile_tr069_enabled(output),
        tr069_ip_index=tr069_ip_index,
    )


def parse_tr069_profile_detail(
    output: str, *, profile_id: int
) -> LiveTr069ProfileDetail:
    """Parse Huawei TR-069 server profile detail enough to confirm existence."""
    lowered = output.lower()
    if "does not exist" in lowered or "failure" in lowered or "unknown command" in lowered:
        return LiveTr069ProfileDetail(profile_id=profile_id, exists=False)

    values = _parse_tr069_profile_detail(output)
    name = (
        values.get("profile-name")
        or values.get("profile name")
        or values.get("name")
        or ""
    )
    acs_url = values.get("url") or values.get("acs url") or values.get("acs-url") or ""
    return LiveTr069ProfileDetail(
        profile_id=profile_id,
        exists=True,
        name=name,
        acs_url=acs_url,
    )


def _open_enabled_olt_shell(olt: OLTDevice):
    from app.services.network import olt_ssh as core

    try:
        transport, channel, policy = core._open_shell(olt)
    except (core.SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None, None, None

    channel.send("enable\n")
    core._read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)
    channel.send("screen-length 0 temporary\n")
    core._read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)
    return True, "Connected.", transport, channel, policy


def _run_live_tr069_profile_command(olt: OLTDevice, *, tr069_profile_id: int):
    from app.services.network import olt_ssh as core

    ok, message, transport, channel, policy = _open_enabled_olt_shell(olt)
    if not ok or transport is None or channel is None or policy is None:
        return False, message, None

    try:
        tr069_output = core._run_huawei_paged_cmd(
            channel,
            f"display ont tr069-server-profile profile-id {tr069_profile_id}",
            prompt=policy.prompt_regex,
        )
        return True, "Live TR-069 profile command completed.", tr069_output
    except Exception as exc:
        logger.exception("Live OLT config-pack audit failed for OLT %s", olt.name)
        return False, f"Live profile read failed: {exc}", None
    finally:
        transport.close()


def suggest_compatible_line_profiles(
    db: Session,
    olt_id: str,
) -> tuple[bool, str, list[CompatibleLineProfileSuggestion]]:
    """Deprecated: line profiles are now imported into OLT mapping tables."""
    olt = db.get(OLTDevice, str(olt_id))
    if olt is None:
        return False, "OLT device not found", []
    pack = resolve_olt_config_pack(db, str(olt.id))
    if pack is None:
        return False, "OLT config pack could not be resolved", []
    del pack
    return (
        False,
        "Line profile suggestions are deprecated; run Import OLT State and use imported mapping coverage.",
        [],
    )


def audit_olt_config_pack_live(db: Session, olt_id: str) -> OltConfigPackLiveAudit:
    """Compare one OLT's saved config pack with live OLT profile output."""
    olt = db.get(OLTDevice, str(olt_id))
    if olt is None:
        return OltConfigPackLiveAudit(
            olt_id=str(olt_id),
            olt_name="unknown",
            success=False,
            errors=["OLT device not found"],
        )

    audit = OltConfigPackLiveAudit(
        olt_id=str(olt.id),
        olt_name=olt.name or str(olt.id),
        success=False,
    )
    pack = resolve_olt_config_pack(db, str(olt.id))
    if pack is None:
        audit.errors.append("OLT config pack could not be resolved")
        return audit

    required = {
        "tr069_olt_profile_id": pack.tr069_olt_profile_id,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        audit.errors.append(
            "Config pack missing fields required for live audit: " + ", ".join(missing)
        )
        return audit

    ok, message, tr069_output = _run_live_tr069_profile_command(
        olt,
        tr069_profile_id=int(pack.tr069_olt_profile_id),
    )
    if not ok or tr069_output is None:
        audit.errors.append(message)
        return audit

    tr069_detail = parse_tr069_profile_detail(
        tr069_output,
        profile_id=int(pack.tr069_olt_profile_id),
    )
    audit.success = True
    audit.observed = {
        "tr069_profile_id": tr069_detail.profile_id,
        "tr069_profile_exists": tr069_detail.exists,
        "tr069_profile_name": tr069_detail.name,
        "tr069_profile_acs_url": tr069_detail.acs_url,
    }

    if not tr069_detail.exists:
        audit.errors.append(
            f"Live OLT TR-069 server profile {pack.tr069_olt_profile_id} was not found"
        )
    audit.warnings.append(
        "Line/service profile and GEM validation is handled by imported OLT state; "
        "run scripts/import_olt_state.py and scripts/report_missing_olt_mappings.py."
    )

    return audit
