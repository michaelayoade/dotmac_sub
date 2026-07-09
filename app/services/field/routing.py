"""Route ordering for assigned field jobs sourced from work-order mirrors."""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Any

from sqlalchemy.orm import Session

from app.models.work_order_mirror import WorkOrderMirror
from app.services.field.jobs import (
    OPEN_STATUSES,
    _location,
    _profile_from_principal,
    _scoped_query,
)

_EARTH_RADIUS_M = 6_371_000.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    r_lat1 = radians(lat1)
    r_lat2 = radians(lat2)
    a = sin(d_lat / 2) ** 2 + cos(r_lat1) * cos(r_lat2) * sin(d_lon / 2) ** 2
    return (2 * _EARTH_RADIUS_M * asin(sqrt(a))) / 1000.0


class FieldRouting:
    @staticmethod
    def order_day_route(
        db: Session,
        principal: dict[str, Any],
        *,
        start_latitude: float,
        start_longitude: float,
    ) -> list[dict]:
        profile = _profile_from_principal(db, principal)
        jobs = (
            _scoped_query(db, profile)
            .filter(WorkOrderMirror.status.in_(OPEN_STATUSES))
            .order_by(
                WorkOrderMirror.scheduled_start.asc().nullslast(),
                WorkOrderMirror.created_at.asc(),
            )
            .all()
        )

        located: list[tuple[WorkOrderMirror, float, float]] = []
        unlocated: list[tuple[WorkOrderMirror, None, None]] = []
        for job in jobs:
            location = _location(job)
            if location.latitude is None or location.longitude is None:
                unlocated.append((job, None, None))
            else:
                located.append(
                    (job, float(location.latitude), float(location.longitude))
                )

        route: list[dict] = []
        current_lat = float(start_latitude)
        current_lng = float(start_longitude)
        total_km = 0.0
        sequence = 0
        remaining = located[:]
        while remaining:
            best_index = min(
                range(len(remaining)),
                key=lambda index: _haversine_km(
                    current_lat,
                    current_lng,
                    remaining[index][1],
                    remaining[index][2],
                ),
            )
            job, job_lat, job_lng = remaining.pop(best_index)
            leg_km = _haversine_km(current_lat, current_lng, job_lat, job_lng)
            total_km += leg_km
            sequence += 1
            route.append(
                _route_stop(
                    job,
                    sequence=sequence,
                    distance_km=round(total_km, 3),
                    leg_km=round(leg_km, 3),
                    latitude=job_lat,
                    longitude=job_lng,
                )
            )
            current_lat, current_lng = job_lat, job_lng

        for job, _, _ in unlocated:
            sequence += 1
            route.append(
                _route_stop(
                    job,
                    sequence=sequence,
                    distance_km=None,
                    leg_km=None,
                    latitude=None,
                    longitude=None,
                )
            )

        return route


def _route_stop(
    job: WorkOrderMirror,
    *,
    sequence: int,
    distance_km: float | None,
    leg_km: float | None,
    latitude: float | None,
    longitude: float | None,
) -> dict:
    return {
        "sequence": sequence,
        "work_order_id": job.crm_work_order_id,
        "work_order_mirror_id": job.id,
        "title": job.title,
        "distance_km": distance_km,
        "leg_km": leg_km,
        "latitude": latitude,
        "longitude": longitude,
        "address_text": job.address,
    }


field_routing = FieldRouting()
