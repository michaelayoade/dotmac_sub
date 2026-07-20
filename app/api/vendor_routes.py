"""Vendor fiber-route GeoJSON API for the admin route-view map.

Mirrors ``app/api/fiber_plant.py``: serves the native vendor
``route_geom`` (LINESTRING, SRID 4326) columns via ``ST_AsGeoJSON`` as a
GeoJSON ``FeatureCollection``. Mounted under ``/api/v1`` and guarded by the
``network:fiber`` permission (see ``app/main.py`` router spec).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import vendor_routes_api

router = APIRouter(prefix="/vendor-routes", tags=["vendor-routes"])


@router.get("/projects/{project_id}/geojson")
def get_project_route_geojson(project_id: str, db: Session = Depends(get_db)):
    """Return an installation project's proposed + as-built routes as GeoJSON."""
    return vendor_routes_api.build_project_route_geojson(db, project_id)
