"""Fiber plant GeoJSON API for map visualization."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from geoalchemy2.functions import ST_AsGeoJSON, ST_X, ST_Y

from app.db import SessionLocal
from app.models.network import (
    FdhCabinet,
    Splitter,
    FiberSpliceClosure,
    FiberSplice,
    FiberSpliceTray,
    FiberSegment,
    FiberTerminationPoint,
)
from app.models.network_monitoring import PopSite

router = APIRouter(prefix="/fiber-plant", tags=["fiber-plant"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/geojson")
def get_fiber_plant_geojson(
    include_fdh: bool = Query(True, description="Include FDH cabinets"),
    include_closures: bool = Query(True, description="Include splice closures"),
    include_pops: bool = Query(True, description="Include POP sites"),
    include_segments: bool = Query(True, description="Include fiber segments/routes"),
    db: Session = Depends(get_db),
):
    """Return all fiber plant assets as a GeoJSON FeatureCollection."""
    features = []

    # FDH Cabinets
    if include_fdh:
        fdh_cabinets = (
            db.query(FdhCabinet)
            .filter(FdhCabinet.is_active.is_(True))
            .filter(FdhCabinet.latitude.isnot(None))
            .filter(FdhCabinet.longitude.isnot(None))
            .all()
        )
        for fdh in fdh_cabinets:
            splitter_count = (
                db.query(func.count(Splitter.id))
                .filter(Splitter.fdh_id == fdh.id)
                .scalar()
            )
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [fdh.longitude, fdh.latitude],
                },
                "properties": {
                    "id": str(fdh.id),
                    "type": "fdh_cabinet",
                    "name": fdh.name,
                    "code": fdh.code,
                    "splitter_count": splitter_count,
                    "notes": fdh.notes,
                },
            })

    # Splice Closures
    if include_closures:
        closures = (
            db.query(FiberSpliceClosure)
            .filter(FiberSpliceClosure.is_active.is_(True))
            .filter(FiberSpliceClosure.latitude.isnot(None))
            .filter(FiberSpliceClosure.longitude.isnot(None))
            .all()
        )
        for closure in closures:
            splice_count = (
                db.query(func.count(FiberSplice.id))
                .filter(FiberSplice.closure_id == closure.id)
                .scalar()
            )
            tray_count = (
                db.query(func.count(FiberSpliceTray.id))
                .filter(FiberSpliceTray.closure_id == closure.id)
                .scalar()
            )
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [closure.longitude, closure.latitude],
                },
                "properties": {
                    "id": str(closure.id),
                    "type": "splice_closure",
                    "name": closure.name,
                    "splice_count": splice_count,
                    "tray_count": tray_count,
                    "notes": closure.notes,
                },
            })

    # POP Sites
    if include_pops:
        pops = (
            db.query(PopSite)
            .filter(PopSite.is_active.is_(True))
            .filter(PopSite.latitude.isnot(None))
            .filter(PopSite.longitude.isnot(None))
            .all()
        )
        for pop in pops:
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [pop.longitude, pop.latitude],
                },
                "properties": {
                    "id": str(pop.id),
                    "type": "pop_site",
                    "name": pop.name,
                    "code": pop.code,
                    "city": pop.city,
                    "notes": pop.notes,
                },
            })

    # Fiber Segments (cable routes)
    if include_segments:
        segments = (
            db.query(FiberSegment)
            .filter(FiberSegment.is_active.is_(True))
            .all()
        )
        for segment in segments:
            # Try to get route geometry, or create from termination points
            coords = None
            if segment.route_geom is not None:
                # Parse the geometry
                geojson = db.query(
                    func.ST_AsGeoJSON(segment.route_geom)
                ).scalar()
                if geojson:
                    import json
                    coords = json.loads(geojson)
            elif segment.from_point and segment.to_point:
                # Create line from termination points
                if (segment.from_point.latitude and segment.from_point.longitude and
                    segment.to_point.latitude and segment.to_point.longitude):
                    coords = {
                        "type": "LineString",
                        "coordinates": [
                            [segment.from_point.longitude, segment.from_point.latitude],
                            [segment.to_point.longitude, segment.to_point.latitude],
                        ],
                    }

            if coords:
                features.append({
                    "type": "Feature",
                    "geometry": coords,
                    "properties": {
                        "id": str(segment.id),
                        "type": "fiber_segment",
                        "name": segment.name,
                        "segment_type": segment.segment_type.value if segment.segment_type else None,
                        "length_m": segment.length_m,
                        "notes": segment.notes,
                    },
                })

    return {
        "type": "FeatureCollection",
        "features": features,
    }


@router.get("/fdh-cabinets/{fdh_id}/splitters")
def get_fdh_splitters(fdh_id: str, db: Session = Depends(get_db)):
    """Get splitters for a specific FDH cabinet."""
    splitters = (
        db.query(Splitter)
        .filter(Splitter.fdh_id == fdh_id)
        .filter(Splitter.is_active.is_(True))
        .all()
    )
    return [
        {
            "id": str(s.id),
            "name": s.name,
            "ratio": s.splitter_ratio,
            "input_ports": s.input_ports,
            "output_ports": s.output_ports,
        }
        for s in splitters
    ]


@router.get("/closures/{closure_id}/splices")
def get_closure_splices(closure_id: str, db: Session = Depends(get_db)):
    """Get splices for a specific closure."""
    splices = (
        db.query(FiberSplice)
        .filter(FiberSplice.closure_id == closure_id)
        .all()
    )
    return [
        {
            "id": str(s.id),
            "splice_type": s.splice_type,
            "loss_db": s.loss_db,
            "tray_id": str(s.tray_id) if s.tray_id else None,
        }
        for s in splices
    ]


@router.get("/stats")
def get_fiber_plant_stats(db: Session = Depends(get_db)):
    """Get summary statistics for the fiber plant."""
    return {
        "fdh_cabinets": db.query(func.count(FdhCabinet.id)).filter(FdhCabinet.is_active.is_(True)).scalar(),
        "fdh_with_location": db.query(func.count(FdhCabinet.id)).filter(
            FdhCabinet.is_active.is_(True),
            FdhCabinet.latitude.isnot(None)
        ).scalar(),
        "splice_closures": db.query(func.count(FiberSpliceClosure.id)).filter(FiberSpliceClosure.is_active.is_(True)).scalar(),
        "closures_with_location": db.query(func.count(FiberSpliceClosure.id)).filter(
            FiberSpliceClosure.is_active.is_(True),
            FiberSpliceClosure.latitude.isnot(None)
        ).scalar(),
        "splitters": db.query(func.count(Splitter.id)).filter(Splitter.is_active.is_(True)).scalar(),
        "fiber_segments": db.query(func.count(FiberSegment.id)).filter(FiberSegment.is_active.is_(True)).scalar(),
        "total_splices": db.query(func.count(FiberSplice.id)).scalar(),
        "pop_sites": db.query(func.count(PopSite.id)).filter(PopSite.is_active.is_(True)).scalar(),
    }
