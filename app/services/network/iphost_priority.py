"""Resolve Huawei IPHOST priority from imported OLT state."""

from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    OltLineProfileGemMapping,
    OltOntRegistration,
    OltServicePort,
)

logger = logging.getLogger(__name__)


def _int_or_none(value: object) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def _only_or_most_common(values: list[int]) -> int | None:
    if not values:
        return None
    counts = Counter(values)
    most_common = counts.most_common()
    if len(most_common) == 1:
        return most_common[0][0]
    top_value, top_count = most_common[0]
    if top_count > most_common[1][1]:
        return top_value
    return None


def _resolve_exact_service_port_gem(
    db: Session,
    *,
    olt_id: object,
    fsp: str,
    ont_id_on_olt: int,
    mgmt_vlan_tag: int,
) -> int | None:
    gems = list(
        db.scalars(
            select(OltServicePort.gem_index).where(
                OltServicePort.olt_device_id == olt_id,
                OltServicePort.fsp == fsp,
                OltServicePort.ont_id_on_olt == ont_id_on_olt,
                OltServicePort.vlan_id == mgmt_vlan_tag,
            )
        ).all()
    )
    return _only_or_most_common([int(gem) for gem in gems])


def _resolve_olt_vlan_gem(
    db: Session,
    *,
    olt_id: object,
    mgmt_vlan_tag: int,
) -> int | None:
    gems = list(
        db.scalars(
            select(OltServicePort.gem_index).where(
                OltServicePort.olt_device_id == olt_id,
                OltServicePort.vlan_id == mgmt_vlan_tag,
            )
        ).all()
    )
    return _only_or_most_common([int(gem) for gem in gems])


def _resolve_registration_line_profile(
    db: Session,
    *,
    olt_id: object,
    fsp: str,
    ont_id_on_olt: int,
) -> int | None:
    value = db.scalars(
        select(OltOntRegistration.line_profile_id).where(
            OltOntRegistration.olt_id == olt_id,
            OltOntRegistration.fsp == fsp,
            OltOntRegistration.ont_id_on_olt == ont_id_on_olt,
            OltOntRegistration.is_active.is_(True),
        )
    ).first()
    return _int_or_none(value)


def _resolve_mapping_priority(
    db: Session,
    *,
    olt_id: object,
    gem_index: int,
    line_profile_id: int | None,
) -> int | None:
    stmt = select(OltLineProfileGemMapping.priority).where(
        OltLineProfileGemMapping.olt_id == olt_id,
        OltLineProfileGemMapping.gem_index == gem_index,
        OltLineProfileGemMapping.priority.is_not(None),
    )
    if line_profile_id is not None:
        stmt = stmt.where(OltLineProfileGemMapping.line_profile_id == line_profile_id)
    priorities = [
        int(priority)
        for priority in db.scalars(stmt).all()
        if priority is not None and 0 <= int(priority) <= 7
    ]
    return _only_or_most_common(priorities)


def resolve_management_iphost_priority(
    db: Session,
    *,
    olt_id: object | None,
    fsp: str | None,
    ont_id_on_olt: int | str | None,
    mgmt_vlan_tag: int | str | None,
    mgmt_gem_index: object | None = None,
    line_profile_id: object | None = None,
) -> int | None:
    """Return the OLT IPHOST priority that matches management service-port GEM.

    Huawei IPHOST priority must match the GEM/priority mapping used by the
    management VLAN service-port. If it does not, TR-069 reachability can break
    even when the IPHOST address, VLAN, gateway, and ACS profile are correct.
    """
    if olt_id is None:
        return None
    vlan = _int_or_none(mgmt_vlan_tag)
    ont_id = _int_or_none(ont_id_on_olt)
    clean_fsp = str(fsp or "").strip()
    if vlan is None:
        return None

    gem_index = None
    if clean_fsp and ont_id is not None:
        gem_index = _resolve_exact_service_port_gem(
            db,
            olt_id=olt_id,
            fsp=clean_fsp,
            ont_id_on_olt=ont_id,
            mgmt_vlan_tag=vlan,
        )
    if gem_index is None:
        gem_index = _int_or_none(mgmt_gem_index)
    if gem_index is None:
        gem_index = _resolve_olt_vlan_gem(db, olt_id=olt_id, mgmt_vlan_tag=vlan)
    if gem_index is None:
        logger.warning(
            "Could not resolve management IPHOST GEM for OLT %s VLAN %s ONT %s/%s",
            olt_id,
            vlan,
            clean_fsp or "-",
            ont_id_on_olt,
        )
        return None

    line_profile = _int_or_none(line_profile_id)
    if line_profile is None and clean_fsp and ont_id is not None:
        line_profile = _resolve_registration_line_profile(
            db,
            olt_id=olt_id,
            fsp=clean_fsp,
            ont_id_on_olt=ont_id,
        )

    priority = _resolve_mapping_priority(
        db,
        olt_id=olt_id,
        gem_index=gem_index,
        line_profile_id=line_profile,
    )
    if priority is None and line_profile is not None:
        priority = _resolve_mapping_priority(
            db,
            olt_id=olt_id,
            gem_index=gem_index,
            line_profile_id=None,
        )
    if priority is None:
        logger.warning(
            "Could not resolve management IPHOST priority for OLT %s GEM %s line profile %s",
            olt_id,
            gem_index,
            line_profile,
        )
        return None
    return priority
