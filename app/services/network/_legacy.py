"""Legacy network services - re-exports from main network.py.

This module re-exports services from the main network.py file that haven't
yet been migrated to their own submodules. These will be moved to proper
submodules in future refactoring:
- Splitter services -> splitters.py
- Fiber services -> fiber/
- PON port splitter links -> olt.py
"""

from fastapi import HTTPException
from sqlalchemy.orm import Session

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
    PonPortSplitterLink,
    Splitter,
    SplitterPort,
    SplitterPortAssignment,
    SplitterPortType,
    FiberEndpointType,
    ODNEndpointType,
)
from app.models.domain_settings import SettingDomain
from app.schemas.network import (
    FdhCabinetCreate,
    FdhCabinetUpdate,
    FiberSegmentCreate,
    FiberSegmentUpdate,
    FiberSpliceClosureCreate,
    FiberSpliceClosureUpdate,
    FiberSpliceCreate,
    FiberSpliceUpdate,
    FiberSpliceTrayCreate,
    FiberSpliceTrayUpdate,
    FiberStrandCreate,
    FiberStrandUpdate,
    FiberTerminationPointCreate,
    FiberTerminationPointUpdate,
    PonPortSplitterLinkCreate,
    PonPortSplitterLinkUpdate,
    SplitterCreate,
    SplitterPortAssignmentCreate,
    SplitterPortAssignmentUpdate,
    SplitterPortCreate,
    SplitterPortUpdate,
    SplitterUpdate,
)
from app.services import settings_spec
from app.services.common import apply_ordering, apply_pagination, validate_enum
from app.services.response import ListResponseMixin


class FdhCabinets(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: FdhCabinetCreate):
        cabinet = FdhCabinet(**payload.model_dump())
        db.add(cabinet)
        db.commit()
        db.refresh(cabinet)
        return cabinet

    @staticmethod
    def get(db: Session, cabinet_id: str):
        cabinet = db.get(FdhCabinet, cabinet_id)
        if not cabinet:
            raise HTTPException(status_code=404, detail="FDH cabinet not found")
        return cabinet

    @staticmethod
    def list(
        db: Session,
        name: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(FdhCabinet)
        if name:
            query = query.filter(FdhCabinet.name.ilike(f"%{name}%"))
        if is_active is None:
            query = query.filter(FdhCabinet.is_active.is_(True))
        else:
            query = query.filter(FdhCabinet.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FdhCabinet.created_at, "name": FdhCabinet.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, cabinet_id: str, payload: FdhCabinetUpdate):
        cabinet = db.get(FdhCabinet, cabinet_id)
        if not cabinet:
            raise HTTPException(status_code=404, detail="FDH cabinet not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(cabinet, key, value)
        db.commit()
        db.refresh(cabinet)
        return cabinet

    @staticmethod
    def delete(db: Session, cabinet_id: str):
        cabinet = db.get(FdhCabinet, cabinet_id)
        if not cabinet:
            raise HTTPException(status_code=404, detail="FDH cabinet not found")
        cabinet.is_active = False
        db.commit()


class Splitters(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SplitterCreate):
        splitter = Splitter(**payload.model_dump())
        db.add(splitter)
        db.commit()
        db.refresh(splitter)
        return splitter

    @staticmethod
    def get(db: Session, splitter_id: str):
        splitter = db.get(Splitter, splitter_id)
        if not splitter:
            raise HTTPException(status_code=404, detail="Splitter not found")
        return splitter

    @staticmethod
    def list(
        db: Session,
        name: str | None,
        fdh_cabinet_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Splitter)
        if name:
            query = query.filter(Splitter.name.ilike(f"%{name}%"))
        if fdh_cabinet_id:
            query = query.filter(Splitter.fdh_cabinet_id == fdh_cabinet_id)
        if is_active is None:
            query = query.filter(Splitter.is_active.is_(True))
        else:
            query = query.filter(Splitter.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Splitter.created_at, "name": Splitter.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, splitter_id: str, payload: SplitterUpdate):
        splitter = db.get(Splitter, splitter_id)
        if not splitter:
            raise HTTPException(status_code=404, detail="Splitter not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(splitter, key, value)
        db.commit()
        db.refresh(splitter)
        return splitter

    @staticmethod
    def delete(db: Session, splitter_id: str):
        splitter = db.get(Splitter, splitter_id)
        if not splitter:
            raise HTTPException(status_code=404, detail="Splitter not found")
        splitter.is_active = False
        db.commit()


class SplitterPorts(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SplitterPortCreate):
        port = SplitterPort(**payload.model_dump())
        db.add(port)
        db.commit()
        db.refresh(port)
        return port

    @staticmethod
    def get(db: Session, port_id: str):
        port = db.get(SplitterPort, port_id)
        if not port:
            raise HTTPException(status_code=404, detail="Splitter port not found")
        return port

    @staticmethod
    def list(
        db: Session,
        splitter_id: str | None,
        port_type: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SplitterPort)
        if splitter_id:
            query = query.filter(SplitterPort.splitter_id == splitter_id)
        if port_type:
            query = query.filter(
                SplitterPort.port_type
                == validate_enum(port_type, SplitterPortType, "port_type")
            )
        if is_active is None:
            query = query.filter(SplitterPort.is_active.is_(True))
        else:
            query = query.filter(SplitterPort.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SplitterPort.created_at, "port_number": SplitterPort.port_number},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, port_id: str, payload: SplitterPortUpdate):
        port = db.get(SplitterPort, port_id)
        if not port:
            raise HTTPException(status_code=404, detail="Splitter port not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(port, key, value)
        db.commit()
        db.refresh(port)
        return port

    @staticmethod
    def delete(db: Session, port_id: str):
        port = db.get(SplitterPort, port_id)
        if not port:
            raise HTTPException(status_code=404, detail="Splitter port not found")
        port.is_active = False
        db.commit()


class SplitterPortAssignments(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SplitterPortAssignmentCreate):
        assignment = SplitterPortAssignment(**payload.model_dump())
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        return assignment

    @staticmethod
    def get(db: Session, assignment_id: str):
        assignment = db.get(SplitterPortAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="Splitter port assignment not found")
        return assignment

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
        if splitter_port_id:
            query = query.filter(SplitterPortAssignment.splitter_port_id == splitter_port_id)
        if is_active is None:
            query = query.filter(SplitterPortAssignment.is_active.is_(True))
        else:
            query = query.filter(SplitterPortAssignment.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SplitterPortAssignment.created_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, assignment_id: str, payload: SplitterPortAssignmentUpdate):
        assignment = db.get(SplitterPortAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="Splitter port assignment not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(assignment, key, value)
        db.commit()
        db.refresh(assignment)
        return assignment

    @staticmethod
    def delete(db: Session, assignment_id: str):
        assignment = db.get(SplitterPortAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="Splitter port assignment not found")
        assignment.is_active = False
        db.commit()


class FiberStrands(ListResponseMixin):
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
            strand.segment_id = segment.id
        return strand

    @staticmethod
    def get(db: Session, strand_id: str):
        strand = db.get(FiberStrand, strand_id)
        if not strand:
            raise HTTPException(status_code=404, detail="Fiber strand not found")
        return strand

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
        if is_active is None:
            query = query.filter(FiberStrand.is_active.is_(True))
        else:
            query = query.filter(FiberStrand.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberStrand.created_at, "strand_number": FiberStrand.strand_number},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, strand_id: str, payload: FiberStrandUpdate):
        strand = db.get(FiberStrand, strand_id)
        if not strand:
            raise HTTPException(status_code=404, detail="Fiber strand not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(strand, key, value)
        db.commit()
        db.refresh(strand)
        return strand

    @staticmethod
    def delete(db: Session, strand_id: str):
        strand = db.get(FiberStrand, strand_id)
        if not strand:
            raise HTTPException(status_code=404, detail="Fiber strand not found")
        strand.is_active = False
        db.commit()


class FiberSpliceClosures(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: FiberSpliceClosureCreate):
        closure = FiberSpliceClosure(**payload.model_dump())
        db.add(closure)
        db.commit()
        db.refresh(closure)
        return closure

    @staticmethod
    def get(db: Session, closure_id: str):
        closure = db.get(FiberSpliceClosure, closure_id)
        if not closure:
            raise HTTPException(status_code=404, detail="Fiber splice closure not found")
        return closure

    @staticmethod
    def list(
        db: Session,
        name: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(FiberSpliceClosure)
        if name:
            query = query.filter(FiberSpliceClosure.name.ilike(f"%{name}%"))
        if is_active is None:
            query = query.filter(FiberSpliceClosure.is_active.is_(True))
        else:
            query = query.filter(FiberSpliceClosure.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberSpliceClosure.created_at, "name": FiberSpliceClosure.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, closure_id: str, payload: FiberSpliceClosureUpdate):
        closure = db.get(FiberSpliceClosure, closure_id)
        if not closure:
            raise HTTPException(status_code=404, detail="Fiber splice closure not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(closure, key, value)
        db.commit()
        db.refresh(closure)
        return closure

    @staticmethod
    def delete(db: Session, closure_id: str):
        closure = db.get(FiberSpliceClosure, closure_id)
        if not closure:
            raise HTTPException(status_code=404, detail="Fiber splice closure not found")
        closure.is_active = False
        db.commit()


class FiberSplices(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: FiberSpliceCreate):
        splice = FiberSplice(**payload.model_dump())
        db.add(splice)
        db.commit()
        db.refresh(splice)
        return splice

    @staticmethod
    def get(db: Session, splice_id: str):
        splice = db.get(FiberSplice, splice_id)
        if not splice:
            raise HTTPException(status_code=404, detail="Fiber splice not found")
        return splice

    @staticmethod
    def list(
        db: Session,
        tray_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(FiberSplice)
        if tray_id:
            query = query.filter(FiberSplice.tray_id == tray_id)
        if is_active is None:
            query = query.filter(FiberSplice.is_active.is_(True))
        else:
            query = query.filter(FiberSplice.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberSplice.created_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, splice_id: str, payload: FiberSpliceUpdate):
        splice = db.get(FiberSplice, splice_id)
        if not splice:
            raise HTTPException(status_code=404, detail="Fiber splice not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(splice, key, value)
        db.commit()
        db.refresh(splice)
        return splice

    @staticmethod
    def delete(db: Session, splice_id: str):
        splice = db.get(FiberSplice, splice_id)
        if not splice:
            raise HTTPException(status_code=404, detail="Fiber splice not found")
        splice.is_active = False
        db.commit()


class FiberSpliceTrays(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: FiberSpliceTrayCreate):
        tray = FiberSpliceTray(**payload.model_dump())
        db.add(tray)
        db.commit()
        db.refresh(tray)
        return tray

    @staticmethod
    def get(db: Session, tray_id: str):
        tray = db.get(FiberSpliceTray, tray_id)
        if not tray:
            raise HTTPException(status_code=404, detail="Fiber splice tray not found")
        return tray

    @staticmethod
    def list(
        db: Session,
        closure_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(FiberSpliceTray)
        if closure_id:
            query = query.filter(FiberSpliceTray.closure_id == closure_id)
        if is_active is None:
            query = query.filter(FiberSpliceTray.is_active.is_(True))
        else:
            query = query.filter(FiberSpliceTray.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberSpliceTray.created_at, "tray_number": FiberSpliceTray.tray_number},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, tray_id: str, payload: FiberSpliceTrayUpdate):
        tray = db.get(FiberSpliceTray, tray_id)
        if not tray:
            raise HTTPException(status_code=404, detail="Fiber splice tray not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(tray, key, value)
        db.commit()
        db.refresh(tray)
        return tray

    @staticmethod
    def delete(db: Session, tray_id: str):
        tray = db.get(FiberSpliceTray, tray_id)
        if not tray:
            raise HTTPException(status_code=404, detail="Fiber splice tray not found")
        tray.is_active = False
        db.commit()


class FiberTerminationPoints(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: FiberTerminationPointCreate):
        point = FiberTerminationPoint(**payload.model_dump())
        db.add(point)
        db.commit()
        db.refresh(point)
        return point

    @staticmethod
    def get(db: Session, point_id: str):
        point = db.get(FiberTerminationPoint, point_id)
        if not point:
            raise HTTPException(status_code=404, detail="Fiber termination point not found")
        return point

    @staticmethod
    def list(
        db: Session,
        strand_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(FiberTerminationPoint)
        if strand_id:
            query = query.filter(FiberTerminationPoint.strand_id == strand_id)
        if is_active is None:
            query = query.filter(FiberTerminationPoint.is_active.is_(True))
        else:
            query = query.filter(FiberTerminationPoint.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberTerminationPoint.created_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, point_id: str, payload: FiberTerminationPointUpdate):
        point = db.get(FiberTerminationPoint, point_id)
        if not point:
            raise HTTPException(status_code=404, detail="Fiber termination point not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(point, key, value)
        db.commit()
        db.refresh(point)
        return point

    @staticmethod
    def delete(db: Session, point_id: str):
        point = db.get(FiberTerminationPoint, point_id)
        if not point:
            raise HTTPException(status_code=404, detail="Fiber termination point not found")
        point.is_active = False
        db.commit()


class FiberSegments(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: FiberSegmentCreate):
        segment = FiberSegment(**payload.model_dump())
        db.add(segment)
        db.commit()
        db.refresh(segment)
        return segment

    @staticmethod
    def get(db: Session, segment_id: str):
        segment = db.get(FiberSegment, segment_id)
        if not segment:
            raise HTTPException(status_code=404, detail="Fiber segment not found")
        return segment

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
        if is_active is None:
            query = query.filter(FiberSegment.is_active.is_(True))
        else:
            query = query.filter(FiberSegment.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": FiberSegment.created_at, "name": FiberSegment.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, segment_id: str, payload: FiberSegmentUpdate):
        segment = db.get(FiberSegment, segment_id)
        if not segment:
            raise HTTPException(status_code=404, detail="Fiber segment not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(segment, key, value)
        db.commit()
        db.refresh(segment)
        return segment

    @staticmethod
    def delete(db: Session, segment_id: str):
        segment = db.get(FiberSegment, segment_id)
        if not segment:
            raise HTTPException(status_code=404, detail="Fiber segment not found")
        segment.is_active = False
        db.commit()


class PonPortSplitterLinks(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PonPortSplitterLinkCreate):
        link = PonPortSplitterLink(**payload.model_dump())
        db.add(link)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def get(db: Session, link_id: str):
        link = db.get(PonPortSplitterLink, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="PON port splitter link not found")
        return link

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
        if pon_port_id:
            query = query.filter(PonPortSplitterLink.pon_port_id == pon_port_id)
        if splitter_id:
            query = query.filter(PonPortSplitterLink.splitter_id == splitter_id)
        if is_active is None:
            query = query.filter(PonPortSplitterLink.is_active.is_(True))
        else:
            query = query.filter(PonPortSplitterLink.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": PonPortSplitterLink.created_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, link_id: str, payload: PonPortSplitterLinkUpdate):
        link = db.get(PonPortSplitterLink, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="PON port splitter link not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(link, key, value)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def delete(db: Session, link_id: str):
        link = db.get(PonPortSplitterLink, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="PON port splitter link not found")
        link.is_active = False
        db.commit()


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
