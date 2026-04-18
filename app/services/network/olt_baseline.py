"""OLT baseline configuration management."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.network import (
    OLTDevice,
    OntProvisioningProfile,
    WanConnectionType,
    WanServiceType,
)
from app.services.network.olt_ssh_profiles import ensure_wan_srvprofile
from app.services.network.provisioning_settings import get_olt_write_mode_enabled

logger = logging.getLogger(__name__)

DEFAULT_WAN_SRVPROFILE_ID = 10
DEFAULT_WAN_SRVPROFILE_NAME = "PPPoE-Internet"


@dataclass
class OltBaselineResult:
    olt_id: str
    olt_name: str
    success: bool
    message: str
    changed_db: bool = False
    changed_olt: bool = False
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)


def _internet_vlan(profile: OntProvisioningProfile) -> int | None:
    if profile.pppoe_omci_vlan:
        return int(profile.pppoe_omci_vlan)
    for service in getattr(profile, "wan_services", []) or []:
        if (
            service.is_active
            and service.service_type == WanServiceType.internet
            and service.connection_type == WanConnectionType.pppoe
            and service.s_vlan
        ):
            return int(service.s_vlan)
    return None


def _active_scoped_profile(
    db: Session,
    olt: OLTDevice,
) -> OntProvisioningProfile | None:
    return db.scalar(
        select(OntProvisioningProfile)
        .options(selectinload(OntProvisioningProfile.wan_services))
        .where(OntProvisioningProfile.olt_device_id == olt.id)
        .where(OntProvisioningProfile.is_active.is_(True))
        .order_by(
            OntProvisioningProfile.is_default.desc(),
            OntProvisioningProfile.updated_at.desc(),
            OntProvisioningProfile.created_at.desc(),
        )
    )


def ensure_olt_baseline(
    db: Session,
    olt_id: str,
    *,
    dry_run: bool = False,
    default_wan_profile_id: int = DEFAULT_WAN_SRVPROFILE_ID,
) -> OltBaselineResult:
    """Ensure one OLT has DB intent and OLT-side WAN baseline config."""
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return OltBaselineResult(
            olt_id=olt_id,
            olt_name="unknown",
            success=False,
            message="OLT not found.",
            dry_run=dry_run,
        )
    if not olt.is_active:
        return OltBaselineResult(
            olt_id=str(olt.id),
            olt_name=olt.name,
            success=True,
            message="Skipped inactive OLT.",
            dry_run=dry_run,
        )

    profile = _active_scoped_profile(db, olt)
    if profile is None:
        return OltBaselineResult(
            olt_id=str(olt.id),
            olt_name=olt.name,
            success=False,
            message="No active OLT-scoped provisioning profile exists.",
            dry_run=dry_run,
        )

    warnings: list[str] = []
    changed_db = False
    internet_vlan = _internet_vlan(profile)
    if internet_vlan is None:
        return OltBaselineResult(
            olt_id=str(olt.id),
            olt_name=olt.name,
            success=False,
            message=f"Profile '{profile.name}' has no PPPoE internet VLAN.",
            dry_run=dry_run,
        )

    if not profile.pppoe_omci_vlan:
        if dry_run:
            warnings.append(
                f"Would set profile '{profile.name}' pppoe_omci_vlan={internet_vlan}."
            )
        else:
            profile.pppoe_omci_vlan = int(internet_vlan)
            profile.updated_at = datetime.now(UTC)
            changed_db = True

    wan_profile_id = int(profile.wan_config_profile_id or 0)
    if dry_run:
        profile_msg = (
            f"ensure ONT WAN profile {wan_profile_id}"
            if wan_profile_id
            else "skip ONT WAN profile; use PPPoE ipconfig/internet-config"
        )
        return OltBaselineResult(
            olt_id=str(olt.id),
            olt_name=olt.name,
            success=True,
            message=(
                f"Dry run: OLT baseline would {profile_msg} for PPPoE VLAN "
                f"{internet_vlan}."
            ),
            changed_db=changed_db,
            dry_run=True,
            warnings=warnings,
        )

    if changed_db:
        db.flush()

    if not get_olt_write_mode_enabled(db):
        return OltBaselineResult(
            olt_id=str(olt.id),
            olt_name=olt.name,
            success=False,
            message="OLT write mode is disabled; DB intent updated but OLT was not changed.",
            changed_db=changed_db,
            dry_run=False,
            warnings=warnings,
        )

    if not wan_profile_id:
        return OltBaselineResult(
            olt_id=str(olt.id),
            olt_name=olt.name,
            success=True,
            message=(
                "OLT baseline ready for PPPoE OMCI via ipconfig/internet-config; "
                "ONT WAN profile is not configured for this OLT."
            ),
            changed_db=changed_db,
            changed_olt=False,
            dry_run=False,
            warnings=warnings,
        )

    ok, msg = ensure_wan_srvprofile(
        olt,
        profile_id=wan_profile_id,
        profile_name=DEFAULT_WAN_SRVPROFILE_NAME,
        vlan_id=int(profile.pppoe_omci_vlan or internet_vlan),
    )
    return OltBaselineResult(
        olt_id=str(olt.id),
        olt_name=olt.name,
        success=ok,
        message=msg,
        changed_db=changed_db,
        changed_olt=ok and "already exists" not in msg.lower(),
        dry_run=False,
        warnings=warnings,
    )


def sync_all_olt_baselines(
    db: Session,
    *,
    dry_run: bool = False,
    default_wan_profile_id: int = DEFAULT_WAN_SRVPROFILE_ID,
) -> list[OltBaselineResult]:
    """Ensure baseline config for every active OLT."""
    olts = list(
        db.scalars(
            select(OLTDevice)
            .where(OLTDevice.is_active.is_(True))
            .order_by(OLTDevice.name)
        ).all()
    )
    results: list[OltBaselineResult] = []
    for olt in olts:
        result = ensure_olt_baseline(
            db,
            str(olt.id),
            dry_run=dry_run,
            default_wan_profile_id=default_wan_profile_id,
        )
        results.append(result)
        logger.info(
            "OLT baseline sync %s: success=%s changed_db=%s changed_olt=%s message=%s",
            olt.name,
            result.success,
            result.changed_db,
            result.changed_olt,
            result.message,
        )
    return results
