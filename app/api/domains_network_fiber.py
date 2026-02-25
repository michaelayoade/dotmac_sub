from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.network import (
    FdhCabinetCreate,
    FdhCabinetRead,
    FdhCabinetUpdate,
    FiberSegmentCreate,
    FiberSegmentRead,
    FiberSegmentUpdate,
    FiberSpliceClosureCreate,
    FiberSpliceClosureRead,
    FiberSpliceClosureUpdate,
    FiberSpliceCreate,
    FiberSpliceRead,
    FiberSpliceTrayCreate,
    FiberSpliceTrayRead,
    FiberSpliceTrayUpdate,
    FiberSpliceUpdate,
    FiberStrandCreate,
    FiberStrandRead,
    FiberStrandUpdate,
    FiberTerminationPointCreate,
    FiberTerminationPointRead,
    FiberTerminationPointUpdate,
    SplitterCreate,
    SplitterPortAssignmentCreate,
    SplitterPortAssignmentRead,
    SplitterPortAssignmentUpdate,
    SplitterPortCreate,
    SplitterPortRead,
    SplitterPortUpdate,
    SplitterRead,
    SplitterUpdate,
)
from app.schemas.network_metrics import FiberPathRead, PortUtilizationRead
from app.services import network as network_service
from app.services.auth_dependencies import require_permission

router = APIRouter()


@router.post(
    "/fdh-cabinets",
    response_model=FdhCabinetRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_fdh_cabinet(payload: FdhCabinetCreate, db: Session = Depends(get_db)):
    return network_service.fdh_cabinets.create(db, payload)


@router.get(
    "/fdh-cabinets/{cabinet_id}",
    response_model=FdhCabinetRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_fdh_cabinet(cabinet_id: str, db: Session = Depends(get_db)):
    return network_service.fdh_cabinets.get(db, cabinet_id)


@router.get(
    "/fdh-cabinets",
    response_model=ListResponse[FdhCabinetRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_fdh_cabinets(
    region_id: str | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.fdh_cabinets.list_response(
        db, region_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/fdh-cabinets/{cabinet_id}",
    response_model=FdhCabinetRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_fdh_cabinet(
    cabinet_id: str, payload: FdhCabinetUpdate, db: Session = Depends(get_db)
):
    return network_service.fdh_cabinets.update(db, cabinet_id, payload)


@router.delete(
    "/fdh-cabinets/{cabinet_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_fdh_cabinet(cabinet_id: str, db: Session = Depends(get_db)):
    network_service.fdh_cabinets.delete(db, cabinet_id)


@router.post(
    "/splitters",
    response_model=SplitterRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_splitter(payload: SplitterCreate, db: Session = Depends(get_db)):
    return network_service.splitters.create(db, payload)


@router.get(
    "/splitters/{splitter_id}",
    response_model=SplitterRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_splitter(splitter_id: str, db: Session = Depends(get_db)):
    return network_service.splitters.get(db, splitter_id)


@router.get(
    "/splitters",
    response_model=ListResponse[SplitterRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_splitters(
    fdh_id: str | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.splitters.list_response(
        db, fdh_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/splitters/{splitter_id}",
    response_model=SplitterRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_splitter(
    splitter_id: str, payload: SplitterUpdate, db: Session = Depends(get_db)
):
    return network_service.splitters.update(db, splitter_id, payload)


@router.delete(
    "/splitters/{splitter_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_splitter(splitter_id: str, db: Session = Depends(get_db)):
    network_service.splitters.delete(db, splitter_id)


@router.post(
    "/splitter-ports",
    response_model=SplitterPortRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_splitter_port(payload: SplitterPortCreate, db: Session = Depends(get_db)):
    return network_service.splitter_ports.create(db, payload)


@router.get(
    "/splitter-ports/{port_id}",
    response_model=SplitterPortRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_splitter_port(port_id: str, db: Session = Depends(get_db)):
    return network_service.splitter_ports.get(db, port_id)


@router.get(
    "/splitter-ports",
    response_model=ListResponse[SplitterPortRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_splitter_ports(
    splitter_id: str | None = None,
    port_type: str | None = None,
    order_by: str = Query(default="port_number"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.splitter_ports.list_response(
        db, splitter_id, port_type, order_by, order_dir, limit, offset
    )


@router.get(
    "/splitter-ports/{splitter_id}/utilization",
    response_model=PortUtilizationRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_splitter_port_utilization(splitter_id: str, db: Session = Depends(get_db)):
    return network_service.splitter_ports.utilization(db, splitter_id)


@router.patch(
    "/splitter-ports/{port_id}",
    response_model=SplitterPortRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_splitter_port(
    port_id: str, payload: SplitterPortUpdate, db: Session = Depends(get_db)
):
    return network_service.splitter_ports.update(db, port_id, payload)


@router.delete(
    "/splitter-ports/{port_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_splitter_port(port_id: str, db: Session = Depends(get_db)):
    network_service.splitter_ports.delete(db, port_id)


@router.post(
    "/splitter-port-assignments",
    response_model=SplitterPortAssignmentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_splitter_port_assignment(
    payload: SplitterPortAssignmentCreate, db: Session = Depends(get_db)
):
    return network_service.splitter_port_assignments.create(db, payload)


@router.get(
    "/splitter-port-assignments/{assignment_id}",
    response_model=SplitterPortAssignmentRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_splitter_port_assignment(assignment_id: str, db: Session = Depends(get_db)):
    return network_service.splitter_port_assignments.get(db, assignment_id)


@router.get(
    "/splitter-port-assignments",
    response_model=ListResponse[SplitterPortAssignmentRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_splitter_port_assignments(
    splitter_port_id: str | None = None,
    subscriber_id: str | None = None,
    account_id: str | None = None,
    subscription_id: str | None = None,
    active: bool | None = None,
    order_by: str = Query(default="assigned_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    resolved_subscriber_id = (
        network_service.splitter_port_assignments.resolve_subscriber_id(
            subscriber_id,
            account_id,
        )
    )
    return network_service.splitter_port_assignments.list_response_with_filters(
        db,
        splitter_port_id,
        resolved_subscriber_id,
        subscription_id,
        active,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/splitter-port-assignments/{assignment_id}",
    response_model=SplitterPortAssignmentRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_splitter_port_assignment(
    assignment_id: str,
    payload: SplitterPortAssignmentUpdate,
    db: Session = Depends(get_db),
):
    return network_service.splitter_port_assignments.update(db, assignment_id, payload)


@router.delete(
    "/splitter-port-assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_splitter_port_assignment(assignment_id: str, db: Session = Depends(get_db)):
    network_service.splitter_port_assignments.delete(db, assignment_id)


@router.post(
    "/fiber-strands",
    response_model=FiberStrandRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_fiber_strand(payload: FiberStrandCreate, db: Session = Depends(get_db)):
    return network_service.fiber_strands.create(db, payload)


@router.get(
    "/fiber-strands/{strand_id}",
    response_model=FiberStrandRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_fiber_strand(strand_id: str, db: Session = Depends(get_db)):
    return network_service.fiber_strands.get(db, strand_id)


@router.get(
    "/fiber-strands",
    response_model=ListResponse[FiberStrandRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_fiber_strands(
    cable_name: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="strand_number"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.fiber_strands.list_response(
        db, cable_name, status, order_by, order_dir, limit, offset
    )


@router.patch(
    "/fiber-strands/{strand_id}",
    response_model=FiberStrandRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_fiber_strand(
    strand_id: str, payload: FiberStrandUpdate, db: Session = Depends(get_db)
):
    return network_service.fiber_strands.update(db, strand_id, payload)


@router.delete(
    "/fiber-strands/{strand_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_fiber_strand(strand_id: str, db: Session = Depends(get_db)):
    network_service.fiber_strands.delete(db, strand_id)


@router.post(
    "/fiber-splice-closures",
    response_model=FiberSpliceClosureRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_fiber_splice_closure(
    payload: FiberSpliceClosureCreate, db: Session = Depends(get_db)
):
    return network_service.fiber_splice_closures.create(db, payload)


@router.get(
    "/fiber-splice-closures/{closure_id}",
    response_model=FiberSpliceClosureRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_fiber_splice_closure(closure_id: str, db: Session = Depends(get_db)):
    return network_service.fiber_splice_closures.get(db, closure_id)


@router.get(
    "/fiber-splice-closures",
    response_model=ListResponse[FiberSpliceClosureRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_fiber_splice_closures(
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.fiber_splice_closures.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/fiber-splice-closures/{closure_id}",
    response_model=FiberSpliceClosureRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_fiber_splice_closure(
    closure_id: str, payload: FiberSpliceClosureUpdate, db: Session = Depends(get_db)
):
    return network_service.fiber_splice_closures.update(db, closure_id, payload)


@router.delete(
    "/fiber-splice-closures/{closure_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_fiber_splice_closure(closure_id: str, db: Session = Depends(get_db)):
    network_service.fiber_splice_closures.delete(db, closure_id)


@router.post(
    "/fiber-splice-trays",
    response_model=FiberSpliceTrayRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_fiber_splice_tray(
    payload: FiberSpliceTrayCreate, db: Session = Depends(get_db)
):
    return network_service.fiber_splice_trays.create(db, payload)


@router.get(
    "/fiber-splice-trays/{tray_id}",
    response_model=FiberSpliceTrayRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_fiber_splice_tray(tray_id: str, db: Session = Depends(get_db)):
    return network_service.fiber_splice_trays.get(db, tray_id)


@router.get(
    "/fiber-splice-trays",
    response_model=ListResponse[FiberSpliceTrayRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_fiber_splice_trays(
    closure_id: str | None = None,
    order_by: str = Query(default="tray_number"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.fiber_splice_trays.list_response(
        db, closure_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/fiber-splice-trays/{tray_id}",
    response_model=FiberSpliceTrayRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_fiber_splice_tray(
    tray_id: str, payload: FiberSpliceTrayUpdate, db: Session = Depends(get_db)
):
    return network_service.fiber_splice_trays.update(db, tray_id, payload)


@router.delete(
    "/fiber-splice-trays/{tray_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_fiber_splice_tray(tray_id: str, db: Session = Depends(get_db)):
    network_service.fiber_splice_trays.delete(db, tray_id)


@router.post(
    "/fiber-splices",
    response_model=FiberSpliceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_fiber_splice(payload: FiberSpliceCreate, db: Session = Depends(get_db)):
    return network_service.fiber_splices.create(db, payload)


@router.get(
    "/fiber-splices/{splice_id}",
    response_model=FiberSpliceRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_fiber_splice(splice_id: str, db: Session = Depends(get_db)):
    return network_service.fiber_splices.get(db, splice_id)


@router.get(
    "/fiber-splices",
    response_model=ListResponse[FiberSpliceRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_fiber_splices(
    closure_id: str | None = None,
    strand_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.fiber_splices.list_response(
        db, closure_id, strand_id, order_by, order_dir, limit, offset
    )


@router.post(
    "/fiber-termination-points",
    response_model=FiberTerminationPointRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_fiber_termination_point(
    payload: FiberTerminationPointCreate, db: Session = Depends(get_db)
):
    return network_service.fiber_termination_points.create(db, payload)


@router.get(
    "/fiber-termination-points/{point_id}",
    response_model=FiberTerminationPointRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_fiber_termination_point(point_id: str, db: Session = Depends(get_db)):
    return network_service.fiber_termination_points.get(db, point_id)


@router.get(
    "/fiber-termination-points",
    response_model=ListResponse[FiberTerminationPointRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_fiber_termination_points(
    endpoint_type: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.fiber_termination_points.list_response(
        db, endpoint_type, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/fiber-termination-points/{point_id}",
    response_model=FiberTerminationPointRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_fiber_termination_point(
    point_id: str, payload: FiberTerminationPointUpdate, db: Session = Depends(get_db)
):
    return network_service.fiber_termination_points.update(db, point_id, payload)


@router.delete(
    "/fiber-termination-points/{point_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_fiber_termination_point(point_id: str, db: Session = Depends(get_db)):
    network_service.fiber_termination_points.delete(db, point_id)


@router.post(
    "/fiber-segments",
    response_model=FiberSegmentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def create_fiber_segment(payload: FiberSegmentCreate, db: Session = Depends(get_db)):
    return network_service.fiber_segments.create(db, payload)


@router.get(
    "/fiber-segments/{segment_id}",
    response_model=FiberSegmentRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def get_fiber_segment(segment_id: str, db: Session = Depends(get_db)):
    return network_service.fiber_segments.get(db, segment_id)


@router.get(
    "/fiber-segments",
    response_model=ListResponse[FiberSegmentRead],
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def list_fiber_segments(
    segment_type: str | None = None,
    fiber_strand_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return network_service.fiber_segments.list_response(
        db, segment_type, fiber_strand_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/fiber-segments/{segment_id}",
    response_model=FiberSegmentRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_fiber_segment(
    segment_id: str, payload: FiberSegmentUpdate, db: Session = Depends(get_db)
):
    return network_service.fiber_segments.update(db, segment_id, payload)


@router.delete(
    "/fiber-segments/{segment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_fiber_segment(segment_id: str, db: Session = Depends(get_db)):
    network_service.fiber_segments.delete(db, segment_id)


@router.get(
    "/fiber-strands/{strand_id}/trace",
    response_model=FiberPathRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:read"))],
)
def trace_fiber_path(
    strand_id: str,
    max_hops: int = Query(default=25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return network_service.fiber_splices.trace_response(db, strand_id, max_hops)


@router.patch(
    "/fiber-splices/{splice_id}",
    response_model=FiberSpliceRead,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def update_fiber_splice(
    splice_id: str, payload: FiberSpliceUpdate, db: Session = Depends(get_db)
):
    return network_service.fiber_splices.update(db, splice_id, payload)


@router.delete(
    "/fiber-splices/{splice_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["network"],
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_fiber_splice(splice_id: str, db: Session = Depends(get_db)):
    network_service.fiber_splices.delete(db, splice_id)
