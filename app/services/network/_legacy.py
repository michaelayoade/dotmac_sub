"""Legacy network services - re-exports from main network.py.

This module re-exports services from the main network.py file that haven't
yet been migrated to their own submodules. These will be moved to proper
submodules in future refactoring:
- Splitter services -> splitters.py
- Fiber services -> fiber/
- PON port splitter links -> olt.py
"""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import (
    FdhCabinet,
    FiberSegment,
    FiberSegmentType,
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
    FiberStrandStatus,
    FiberTerminationPoint,
    ODNEndpointType,
    PonPortSplitterLink,
    Splitter,
    SplitterPort,
    SplitterPortAssignment,
    SplitterPortType,
)
from app.schemas.network import (
    FdhCabinetUpdate,
    FiberSegmentUpdate,
    FiberSpliceClosureUpdate,
    FiberSpliceTrayUpdate,
    FiberSpliceUpdate,
    FiberStrandCreate,
    FiberStrandUpdate,
    FiberTerminationPointUpdate,
    PonPortSplitterLinkUpdate,
    SplitterPortAssignmentUpdate,
    SplitterPortUpdate,
    SplitterUpdate,
)
from app.services import settings_spec
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
        query = apply_optional_equals(
            query, {Splitter.fdh_id: coerce_uuid(fdh_cabinet_id)}
        )
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
        query = apply_optional_equals(
            query, {SplitterPort.splitter_id: coerce_uuid(splitter_id)}
        )
        if port_type:
            query = query.filter(
                SplitterPort.port_type
                == validate_enum(port_type, SplitterPortType, "port_type")
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
    soft_delete_field = "is_active"
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


class FiberStrands(CRUDManager[FiberStrand]):
    model = FiberStrand
    not_found_detail = "Fiber strand not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def create(db: Session, payload: FiberStrandCreate):
        segment = None
        if payload.segment_id:
            segment = db.get(FiberSegment, payload.segment_id)
            if not segment:
                raise HTTPException(status_code=404, detail="Fiber segment not found")
        data = payload.model_dump(exclude={"segment_id"})
        if segment and (not payload.cable_name or payload.cable_name.startswith("segment-")):
            data["cable_name"] = segment.name
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.network, "default_fiber_strand_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, FiberStrandStatus, "status"
                )
        strand = FiberStrand(**data)
        db.add(strand)
        db.commit()
        db.refresh(strand)
        if segment:
            # Link segment <-> strand for callers expecting `strand.segment_id`.
            segment.fiber_strand_id = strand.id
            db.commit()
            db.refresh(strand)
        return strand

    @classmethod
    def get(cls, db: Session, strand_id: str):
        return super().get(db, strand_id)

    @staticmethod
    def list(
        db: Session,
        cable_name: str | None = None,
        status: str | None = None,
        order_by: str = "created_at",
        order_dir: str = "asc",
        limit: int = 100,
        offset: int = 0,
        segment_id: str | None = None,
        is_active: bool | None = None,
    ):
        query = db.query(FiberStrand)
        if segment_id:
            segment = db.get(FiberSegment, segment_id)
            if not segment:
                raise HTTPException(status_code=404, detail="Fiber segment not found")
            query = query.filter(FiberStrand.cable_name == segment.name)
        if cable_name:
            query = query.filter(FiberStrand.cable_name == cable_name)
        if status:
            query = query.filter(
                FiberStrand.status
                == validate_enum(status, FiberStrandStatus, "status")
            )
        query = apply_active_state(query, FiberStrand.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberStrand.created_at, "strand_number": FiberStrand.strand_number},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def update(cls, db: Session, strand_id: str, payload: FiberStrandUpdate):
        return super().update(db, strand_id, payload)

    @classmethod
    def delete(cls, db: Session, strand_id: str):
        return super().delete(db, strand_id)


class FiberSpliceClosures(CRUDManager[FiberSpliceClosure]):
    model = FiberSpliceClosure
    not_found_detail = "Fiber splice closure not found"
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
        is_active: bool | None = None,
    ):
        query = db.query(FiberSpliceClosure)
        query = apply_optional_ilike(query, {FiberSpliceClosure.name: name})
        query = apply_active_state(query, FiberSpliceClosure.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberSpliceClosure.created_at, "name": FiberSpliceClosure.name},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, closure_id: str):
        return super().get(db, closure_id)

    @classmethod
    def update(cls, db: Session, closure_id: str, payload: FiberSpliceClosureUpdate):
        return super().update(db, closure_id, payload)

    @classmethod
    def delete(cls, db: Session, closure_id: str):
        return super().delete(db, closure_id)


class FiberSplices(CRUDManager[FiberSplice]):
    model = FiberSplice
    not_found_detail = "Fiber splice not found"
    soft_delete_field = None

    @staticmethod
    def list(
        db: Session,
        order_by: str = "created_at",
        order_dir: str = "asc",
        limit: int = 20,
        offset: int = 0,
        tray_id: str | None = None,
    ):
        query = db.query(FiberSplice)
        query = apply_optional_equals(query, {FiberSplice.tray_id: coerce_uuid(tray_id)})
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberSplice.created_at},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, splice_id: str):
        return super().get(db, splice_id)

    @classmethod
    def update(cls, db: Session, splice_id: str, payload: FiberSpliceUpdate):
        return super().update(db, splice_id, payload)

    @classmethod
    def delete(cls, db: Session, splice_id: str):
        return super().delete(db, splice_id)

    @staticmethod
    def trace_response(db: Session, strand_id: str, max_hops: int = 25) -> dict[str, object]:
        """Build a minimal fiber path response for a strand.

        Note: This is intentionally conservative. It returns nearby splices for the
        strand, suitable for the API response model.
        """
        try:
            strand_uuid = UUID(strand_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid strand_id") from exc

        # Find splices touching this strand.
        splices = (
            db.query(FiberSplice)
            .filter(
                (FiberSplice.from_strand_id == strand_uuid)
                | (FiberSplice.to_strand_id == strand_uuid)
            )
            .limit(max_hops)
            .all()
        )

        segments: list[dict[str, object]] = []
        for splice in splices:
            segments.append(
                {
                    "segment_type": "splice",
                    "strand_id": strand_id,
                    "splice_id": str(splice.id),
                    "closure_id": str(splice.closure_id),
                    "upstream": None,
                    "downstream": None,
                }
            )

        return {"segments": segments}


class FiberSpliceTrays(CRUDManager[FiberSpliceTray]):
    model = FiberSpliceTray
    not_found_detail = "Fiber splice tray not found"
    soft_delete_field = None

    @staticmethod
    def list(
        db: Session,
        order_by: str = "created_at",
        order_dir: str = "asc",
        limit: int = 20,
        offset: int = 0,
        closure_id: str | None = None,
    ):
        query = db.query(FiberSpliceTray)
        query = apply_optional_equals(
            query, {FiberSpliceTray.closure_id: coerce_uuid(closure_id)}
        )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberSpliceTray.created_at, "tray_number": FiberSpliceTray.tray_number},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, tray_id: str):
        return super().get(db, tray_id)

    @classmethod
    def update(cls, db: Session, tray_id: str, payload: FiberSpliceTrayUpdate):
        return super().update(db, tray_id, payload)

    @classmethod
    def delete(cls, db: Session, tray_id: str):
        return super().delete(db, tray_id)


class FiberTerminationPoints(CRUDManager[FiberTerminationPoint]):
    model = FiberTerminationPoint
    not_found_detail = "Fiber termination point not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def list(
        db: Session,
        order_by: str = "created_at",
        order_dir: str = "asc",
        limit: int = 20,
        offset: int = 0,
        endpoint_type: str | None = None,
        is_active: bool | None = None,
    ):
        query = db.query(FiberTerminationPoint)
        if endpoint_type:
            query = query.filter(
                FiberTerminationPoint.endpoint_type
                == validate_enum(endpoint_type, ODNEndpointType, "endpoint_type")
            )
        query = apply_active_state(query, FiberTerminationPoint.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberTerminationPoint.created_at},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, point_id: str):
        return super().get(db, point_id)

    @classmethod
    def update(cls, db: Session, point_id: str, payload: FiberTerminationPointUpdate):
        return super().update(db, point_id, payload)

    @classmethod
    def delete(cls, db: Session, point_id: str):
        return super().delete(db, point_id)


class FiberSegments(CRUDManager[FiberSegment]):
    model = FiberSegment
    not_found_detail = "Fiber segment not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def list(
        db: Session,
        segment_type: str | None,
        fiber_strand_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(FiberSegment)
        if segment_type:
            query = query.filter(
                FiberSegment.segment_type
                == validate_enum(segment_type, FiberSegmentType, "segment_type")
            )
        if fiber_strand_id:
            query = query.filter(FiberSegment.fiber_strand_id == fiber_strand_id)
        query = apply_active_state(query, FiberSegment.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberSegment.created_at, "name": FiberSegment.name},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, segment_id: str):
        return super().get(db, segment_id)

    @classmethod
    def update(cls, db: Session, segment_id: str, payload: FiberSegmentUpdate):
        return super().update(db, segment_id, payload)

    @classmethod
    def delete(cls, db: Session, segment_id: str):
        return super().delete(db, segment_id)


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


# Service instances
fdh_cabinets = FdhCabinets()
splitters = Splitters()
splitter_ports = SplitterPorts()
splitter_port_assignments = SplitterPortAssignments()
fiber_strands = FiberStrands()
fiber_splice_closures = FiberSpliceClosures()
fiber_splices = FiberSplices()
fiber_splice_trays = FiberSpliceTrays()
fiber_termination_points = FiberTerminationPoints()
fiber_segments = FiberSegments()
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
    "FiberStrands",
    "fiber_strands",
    "FiberSpliceClosures",
    "fiber_splice_closures",
    "FiberSplices",
    "fiber_splices",
    "FiberSpliceTrays",
    "fiber_splice_trays",
    "FiberTerminationPoints",
    "fiber_termination_points",
    "FiberSegments",
    "fiber_segments",
    "PonPortSplitterLinks",
    "pon_port_splitter_links",
]
