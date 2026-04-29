"""Single ACS resolution policy for ONT and OLT workflows."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import OLTDevice, OntUnit, PonPort
from app.models.tr069 import Tr069AcsServer
from app.services import settings_spec


@dataclass(frozen=True)
class AcsResolution:
    server: Tr069AcsServer | None
    source: str

    @property
    def server_id(self) -> str | None:
        return str(self.server.id) if self.server is not None else None


def _active_server(db: Session, server_id: object | None) -> Tr069AcsServer | None:
    if not server_id:
        return None
    server = db.get(Tr069AcsServer, str(server_id))
    if server and server.is_active and server.base_url:
        return server
    return None


def _olt_for_ont(
    db: Session,
    ont: OntUnit,
    *,
    olt: OLTDevice | None = None,
    olt_id: str | None = None,
) -> OLTDevice | None:
    if olt is not None:
        return olt
    if olt_id:
        return db.get(OLTDevice, str(olt_id))
    if getattr(ont, "olt_device_id", None):
        found = db.get(OLTDevice, str(ont.olt_device_id))
        if found is not None:
            return found

    for assignment in getattr(ont, "assignments", []):
        if not getattr(assignment, "active", False) or not assignment.pon_port_id:
            continue
        pon_port = db.get(PonPort, str(assignment.pon_port_id))
        if pon_port and pon_port.olt_id:
            return db.get(OLTDevice, str(pon_port.olt_id))
    return None


def resolve_operational_acs(
    db: Session,
    *,
    olt: OLTDevice | None = None,
    olt_id: str | None = None,
    allow_single_active: bool = False,
) -> AcsResolution:
    """Resolve an ACS outside ONT context.

    This is for OLT/admin pages and new ONT authorization. ONT-specific policy is
    handled by ``resolve_acs_for_ont``.
    """
    if olt is None and olt_id:
        olt = db.get(OLTDevice, str(olt_id))

    if olt is not None:
        server = _active_server(db, getattr(olt, "tr069_acs_server_id", None))
        if server is not None:
            return AcsResolution(server, "olt_default")

    default_server_id = settings_spec.resolve_value(
        db,
        SettingDomain.tr069,
        "default_acs_server_id",
    )
    server = _active_server(db, default_server_id)
    if server is not None:
        return AcsResolution(server, "system_default")

    if allow_single_active:
        active_servers = list(
            db.scalars(
                select(Tr069AcsServer)
                .where(Tr069AcsServer.is_active.is_(True))
                .order_by(Tr069AcsServer.name)
            ).all()
        )
        if len(active_servers) == 1 and active_servers[0].base_url:
            return AcsResolution(active_servers[0], "single_active")

    return AcsResolution(None, "not_configured")


def resolve_acs_for_ont(
    db: Session,
    ont: OntUnit,
    *,
    olt: OLTDevice | None = None,
    olt_id: str | None = None,
    allow_single_active: bool = False,
) -> AcsResolution:
    """Resolve desired ACS for an ONT.

    Policy is intentionally small: ONT explicit override, then OLT default, then
    system default. Observed GenieACS links are not desired-state authority.
    """
    server = _active_server(db, getattr(ont, "tr069_acs_server_id", None))
    if server is not None:
        return AcsResolution(server, "ont_override")

    resolved_olt = _olt_for_ont(db, ont, olt=olt, olt_id=olt_id)
    return resolve_operational_acs(
        db,
        olt=resolved_olt,
        allow_single_active=allow_single_active,
    )
