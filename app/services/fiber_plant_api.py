"""Service helpers for fiber plant API endpoints."""

from __future__ import annotations

import json

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.network import (
    FdhCabinet,
    FiberSegment,
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    Splitter,
)
from app.models.network_monitoring import PopSite


def build_fiber_plant_geojson(
    db: Session,
    *,
    include_fdh: bool,
    include_closures: bool,
    include_pops: bool,
    include_segments: bool,
) -> dict:
    """Build fiber plant assets as a GeoJSON FeatureCollection."""
    features: list[dict] = []

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
            features.append(
                {
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
                }
            )

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
            features.append(
                {
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
                }
            )

    if include_pops:
        pops = (
            db.query(PopSite)
            .filter(PopSite.is_active.is_(True))
            .filter(PopSite.latitude.isnot(None))
            .filter(PopSite.longitude.isnot(None))
            .all()
        )
        for pop in pops:
            features.append(
                {
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
                }
            )

    if include_segments:
        segments = db.query(FiberSegment).filter(FiberSegment.is_active.is_(True)).all()
        for segment in segments:
            coords = None
            if segment.route_geom is not None:
                geojson = db.query(func.ST_AsGeoJSON(segment.route_geom)).scalar()
                if geojson:
                    coords = json.loads(geojson)
            elif segment.from_point and segment.to_point:
                if (
                    segment.from_point.latitude
                    and segment.from_point.longitude
                    and segment.to_point.latitude
                    and segment.to_point.longitude
                ):
                    coords = {
                        "type": "LineString",
                        "coordinates": [
                            [segment.from_point.longitude, segment.from_point.latitude],
                            [segment.to_point.longitude, segment.to_point.latitude],
                        ],
                    }

            if coords:
                features.append(
                    {
                        "type": "Feature",
                        "geometry": coords,
                        "properties": {
                            "id": str(segment.id),
                            "type": "fiber_segment",
                            "name": segment.name,
                            "segment_type": (
                                segment.segment_type.value
                                if segment.segment_type
                                else None
                            ),
                            "length_m": segment.length_m,
                            "notes": segment.notes,
                        },
                    }
                )

    return {"type": "FeatureCollection", "features": features}


def list_fdh_splitters(db: Session, fdh_id: str) -> list[dict]:
    """Return active splitters attached to a cabinet."""
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


def list_closure_splices(db: Session, closure_id: str) -> list[dict]:
    """Return splices attached to a closure."""
    splices = db.query(FiberSplice).filter(FiberSplice.closure_id == closure_id).all()
    return [
        {
            "id": str(s.id),
            "splice_type": s.splice_type,
            "loss_db": s.loss_db,
            "tray_id": str(s.tray_id) if s.tray_id else None,
        }
        for s in splices
    ]


def get_fiber_plant_stats(db: Session) -> dict:
    """Return summary statistics for fiber plant assets."""
    return {
        "fdh_cabinets": db.query(func.count(FdhCabinet.id))
        .filter(FdhCabinet.is_active.is_(True))
        .scalar(),
        "fdh_with_location": db.query(func.count(FdhCabinet.id))
        .filter(FdhCabinet.is_active.is_(True), FdhCabinet.latitude.isnot(None))
        .scalar(),
        "splice_closures": db.query(func.count(FiberSpliceClosure.id))
        .filter(FiberSpliceClosure.is_active.is_(True))
        .scalar(),
        "closures_with_location": db.query(func.count(FiberSpliceClosure.id))
        .filter(
            FiberSpliceClosure.is_active.is_(True),
            FiberSpliceClosure.latitude.isnot(None),
        )
        .scalar(),
        "splitters": db.query(func.count(Splitter.id))
        .filter(Splitter.is_active.is_(True))
        .scalar(),
        "fiber_segments": db.query(func.count(FiberSegment.id))
        .filter(FiberSegment.is_active.is_(True))
        .scalar(),
        "total_splices": db.query(func.count(FiberSplice.id)).scalar(),
        "pop_sites": db.query(func.count(PopSite.id))
        .filter(PopSite.is_active.is_(True))
        .scalar(),
    }
