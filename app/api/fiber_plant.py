"""Fiber plant GeoJSON API for map visualization."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import fiber_plant_api

router = APIRouter(prefix="/fiber-plant", tags=["fiber-plant"])


@router.get("/geojson")
def get_fiber_plant_geojson(
    include_fdh: bool = Query(True, description="Include FDH cabinets"),
    include_closures: bool = Query(True, description="Include splice closures"),
    include_pops: bool = Query(True, description="Include POP sites"),
    include_segments: bool = Query(True, description="Include fiber segments/routes"),
    db: Session = Depends(get_db),
):
    """Return all fiber plant assets as a GeoJSON FeatureCollection."""
    return fiber_plant_api.build_fiber_plant_geojson(
        db,
        include_fdh=include_fdh,
        include_closures=include_closures,
        include_pops=include_pops,
        include_segments=include_segments,
    )


@router.get("/fdh-cabinets/{fdh_id}/splitters")
def get_fdh_splitters(fdh_id: str, db: Session = Depends(get_db)):
    """Get splitters for a specific FDH cabinet."""
    return fiber_plant_api.list_fdh_splitters(db, fdh_id)


@router.get("/closures/{closure_id}/splices")
def get_closure_splices(closure_id: str, db: Session = Depends(get_db)):
    """Get splices for a specific closure."""
    return fiber_plant_api.list_closure_splices(db, closure_id)


@router.get("/stats")
def get_fiber_plant_stats(db: Session = Depends(get_db)):
    """Get summary statistics for the fiber plant."""
    return fiber_plant_api.get_fiber_plant_stats(db)
