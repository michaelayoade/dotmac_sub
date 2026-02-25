"""Splitter-related network services."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.network import (
    FdhCabinet,
    PonPortSplitterLink,
    Splitter,
    SplitterPort,
    SplitterPortAssignment,
    SplitterPortType,
)
from app.schemas.network import (
    FdhCabinetUpdate,
    PonPortSplitterLinkUpdate,
    SplitterPortAssignmentUpdate,
    SplitterPortUpdate,
    SplitterUpdate,
)
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    validate_enum,
)
from app.services.crud import CRUDManager
from app.services.query_builders import (
    apply_active_state,
    apply_optional_equals,
    apply_optional_ilike,
)


class FdhCabinets(CRUDManager[FdhCabinet]):
    model = FdhCabinet
    not_found_detail = "FDH cabinet not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def list(
        db: Session,
        order_by: str = "created_at",
        order_dir: str = "asc",
        limit: int = 20,
        offset: int = 0,
        name: str | None = None,
        region_id: str | None = None,
        is_active: bool | None = None,
    ):
        query = db.query(FdhCabinet)
        query = apply_optional_ilike(query, {FdhCabinet.name: name})
        if region_id:
            query = query.filter(FdhCabinet.region_id == coerce_uuid(region_id))
        query = apply_active_state(query, FdhCabinet.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FdhCabinet.created_at, "name": FdhCabinet.name},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, cabinet_id: str):
        return super().get(db, cabinet_id)

    @classmethod
    def update(cls, db: Session, cabinet_id: str, payload: FdhCabinetUpdate):
        return super().update(db, cabinet_id, payload)

    @classmethod
    def delete(cls, db: Session, cabinet_id: str):
        return super().delete(db, cabinet_id)


class Splitters(CRUDManager[Splitter]):
    model = Splitter
    not_found_detail = "Splitter not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def list(
        db: Session,
        order_by: str = "created_at",
        order_dir: str = "asc",
        limit: int = 20,
        offset: int = 0,
        name: str | None = None,
        fdh_cabinet_id: str | None = None,
        # Backwards-compat alias used by older tests/callers.
        fdh_id: str | None = None,
        is_active: bool | None = None,
    ):
        if fdh_id and not fdh_cabinet_id:
            fdh_cabinet_id = fdh_id
        query = db.query(Splitter)
        query = apply_optional_ilike(query, {Splitter.name: name})
        query = apply_optional_equals(query, {Splitter.fdh_id: coerce_uuid(fdh_cabinet_id)})
        query = apply_active_state(query, Splitter.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Splitter.created_at, "name": Splitter.name},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, splitter_id: str):
        return super().get(db, splitter_id)

    @classmethod
    def update(cls, db: Session, splitter_id: str, payload: SplitterUpdate):
        return super().update(db, splitter_id, payload)

    @classmethod
    def delete(cls, db: Session, splitter_id: str):
        return super().delete(db, splitter_id)


class SplitterPorts(CRUDManager[SplitterPort]):
    model = SplitterPort
    not_found_detail = "Splitter port not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def list(
        db: Session,
        order_by: str = "created_at",
        order_dir: str = "asc",
        limit: int = 20,
        offset: int = 0,
        splitter_id: str | None = None,
        port_type: str | None = None,
        is_active: bool | None = None,
    ):
        query = db.query(SplitterPort)
        query = apply_optional_equals(query, {SplitterPort.splitter_id: coerce_uuid(splitter_id)})
        if port_type:
            query = query.filter(
                SplitterPort.port_type == validate_enum(port_type, SplitterPortType, "port_type")
            )
        query = apply_active_state(query, SplitterPort.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SplitterPort.created_at, "port_number": SplitterPort.port_number},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, port_id: str):
        return super().get(db, port_id)

    @classmethod
    def update(cls, db: Session, port_id: str, payload: SplitterPortUpdate):
        return super().update(db, port_id, payload)

    @classmethod
    def delete(cls, db: Session, port_id: str):
        return super().delete(db, port_id)

    @staticmethod
    def utilization(db: Session, splitter_id: str | None):
        """Return splitter port utilization summary."""
        splitter_uuid: UUID | None = None
        if splitter_id:
            try:
                splitter_uuid = UUID(splitter_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid splitter_id") from exc

        ports_query = db.query(SplitterPort).filter(SplitterPort.is_active.is_(True))
        if splitter_uuid:
            ports_query = ports_query.filter(SplitterPort.splitter_id == splitter_uuid)
        total_ports = ports_query.count()

        assigned_query = (
            db.query(SplitterPortAssignment.splitter_port_id)
            .join(SplitterPort, SplitterPort.id == SplitterPortAssignment.splitter_port_id)
            .filter(SplitterPortAssignment.active.is_(True))
        )
        if splitter_uuid:
            assigned_query = assigned_query.filter(SplitterPort.splitter_id == splitter_uuid)
        assigned_ports = assigned_query.distinct().count()

        return {
            "splitter_id": splitter_id,
            "total_ports": total_ports,
            "assigned_ports": assigned_ports,
        }


class SplitterPortAssignments(CRUDManager[SplitterPortAssignment]):
    model = SplitterPortAssignment
    not_found_detail = "Splitter port assignment not found"
    soft_delete_field = "active"
    soft_delete_value = False

    @staticmethod
    def list(
        db: Session,
        splitter_port_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SplitterPortAssignment)
        query = apply_optional_equals(
            query,
            {SplitterPortAssignment.splitter_port_id: splitter_port_id},
        )
        query = apply_active_state(query, SplitterPortAssignment.active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SplitterPortAssignment.created_at},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, assignment_id: str):
        return super().get(db, assignment_id)

    @classmethod
    def update(cls, db: Session, assignment_id: str, payload: SplitterPortAssignmentUpdate):
        return super().update(db, assignment_id, payload)

    @classmethod
    def delete(cls, db: Session, assignment_id: str):
        return super().delete(db, assignment_id)


class PonPortSplitterLinks(CRUDManager[PonPortSplitterLink]):
    model = PonPortSplitterLink
    not_found_detail = "PON port splitter link not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def list(
        db: Session,
        pon_port_id: str | None,
        splitter_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PonPortSplitterLink)
        query = apply_optional_equals(
            query,
            {
                PonPortSplitterLink.pon_port_id: pon_port_id,
                PonPortSplitterLink.splitter_port_id: splitter_id,
            },
        )
        query = apply_active_state(query, PonPortSplitterLink.active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": PonPortSplitterLink.created_at},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, link_id: str):
        return super().get(db, link_id)

    @classmethod
    def update(cls, db: Session, link_id: str, payload: PonPortSplitterLinkUpdate):
        return super().update(db, link_id, payload)

    @classmethod
    def delete(cls, db: Session, link_id: str):
        return super().delete(db, link_id)


fdh_cabinets = FdhCabinets()
splitters = Splitters()
splitter_ports = SplitterPorts()
splitter_port_assignments = SplitterPortAssignments()
pon_port_splitter_links = PonPortSplitterLinks()

__all__ = [
    "FdhCabinets",
    "fdh_cabinets",
    "Splitters",
    "splitters",
    "SplitterPorts",
    "splitter_ports",
    "SplitterPortAssignments",
    "splitter_port_assignments",
    "PonPortSplitterLinks",
    "pon_port_splitter_links",
]
