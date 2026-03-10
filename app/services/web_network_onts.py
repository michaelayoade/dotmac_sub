"""Web service helpers for ONT form dropdowns and context."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    OLTDevice,
    PonType,
    Splitter,
    Vlan,
)
from app.models.tr069 import Tr069AcsServer
from app.services.network.onu_types import onu_types
from app.services.network.speed_profiles import speed_profiles
from app.services.network.zones import network_zones

logger = logging.getLogger(__name__)


def get_onu_types(db: Session) -> list[Any]:
    """Fetch active ONU types for form dropdowns."""
    return onu_types.list(db, is_active=True)


def get_olt_devices(db: Session) -> list[OLTDevice]:
    """Fetch active OLT devices for form dropdowns."""
    stmt = (
        select(OLTDevice)
        .where(OLTDevice.is_active.is_(True))
        .order_by(OLTDevice.name)
    )
    return list(db.scalars(stmt).all())


def get_vlans(db: Session) -> list[Vlan]:
    """Fetch VLANs for form dropdowns."""
    stmt = select(Vlan).order_by(Vlan.tag)
    return list(db.scalars(stmt).all())


def get_zones(db: Session) -> list[Any]:
    """Fetch active network zones for form dropdowns."""
    return network_zones.list(db, is_active=True)


def get_splitters(db: Session) -> list[Splitter]:
    """Fetch splitters for form dropdowns."""
    stmt = (
        select(Splitter)
        .where(Splitter.is_active.is_(True))
        .order_by(Splitter.name)
    )
    return list(db.scalars(stmt).all())


def get_speed_profiles(db: Session, direction: str) -> list[Any]:
    """Fetch speed profiles for a given direction (download/upload)."""
    return speed_profiles.list(db, direction=direction, is_active=True)


def get_tr069_servers(db: Session) -> list[Tr069AcsServer]:
    """Fetch active TR069 ACS servers for form dropdowns."""
    stmt = (
        select(Tr069AcsServer)
        .where(Tr069AcsServer.is_active.is_(True))
        .order_by(Tr069AcsServer.name)
    )
    return list(db.scalars(stmt).all())


def ont_form_dependencies(db: Session) -> dict[str, Any]:
    """Build all dropdown data needed by the ONT provisioning form."""
    return {
        "onu_types": get_onu_types(db),
        "olt_devices": get_olt_devices(db),
        "vlans": get_vlans(db),
        "zones": get_zones(db),
        "splitters": get_splitters(db),
        "speed_profiles_download": get_speed_profiles(db, "download"),
        "speed_profiles_upload": get_speed_profiles(db, "upload"),
        "pon_types": [e.value for e in PonType],
    }
