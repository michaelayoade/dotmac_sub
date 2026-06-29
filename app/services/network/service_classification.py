"""Network service classification settings."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import settings_spec

DEFAULT_INTERNET_SERVICE_VLANS = frozenset({203})


def parse_vlan_list(value: object | None) -> set[int]:
    if value is None:
        return set(DEFAULT_INTERNET_SERVICE_VLANS)
    vlans: set[int] = set()
    for part in str(value).replace(";", ",").split(","):
        raw = part.strip()
        if not raw:
            continue
        try:
            vlan = int(raw)
        except ValueError:
            continue
        if 1 <= vlan <= 4094:
            vlans.add(vlan)
    return vlans or set(DEFAULT_INTERNET_SERVICE_VLANS)


def internet_service_vlans(db: Session) -> set[int]:
    value = settings_spec.resolve_value(
        db, SettingDomain.network, "internet_service_vlans"
    )
    return parse_vlan_list(value)


def service_type_for_vlan(db: Session, vlan_id: int | None) -> str:
    return "internet" if vlan_id in internet_service_vlans(db) else "management"
