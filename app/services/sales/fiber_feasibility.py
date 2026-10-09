"""Typed, read-only initial Fiber feasibility owner."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from geoalchemy2.functions import ST_MakePoint, ST_SetSRID
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import FiberAccessPoint
from app.services import settings_spec
from app.services.domain_errors import DomainError


class FiberFeasibilityStatus(StrEnum):
    covered = "covered"
    survey_required = "survey_required"
    out_of_area = "out_of_area"


@dataclass(frozen=True, slots=True)
class FiberFeasibilityQuery:
    latitude: Decimal
    longitude: Decimal

    def __post_init__(self) -> None:
        if (
            not self.latitude.is_finite()
            or not self.longitude.is_finite()
            or not Decimal(-90) <= self.latitude <= Decimal(90)
            or not Decimal(-180) <= self.longitude <= Decimal(180)
        ):
            raise DomainError(
                code="sales.fiber_feasibility.invalid_coordinates",
                message="Select valid installation coordinates.",
            )


@dataclass(frozen=True, slots=True)
class FiberFeasibilityResult:
    feasible: bool
    coverage: FiberFeasibilityStatus
    nearest_fap_id: UUID | None = None
    nearest_fap_name: str | None = None
    distance_meters: Decimal | None = None

    def as_metadata(self) -> dict[str, object]:
        """Serialize only at the existing Quote metadata/reporting boundary."""
        return {
            "feasible": self.feasible,
            "coverage": self.coverage.value,
            "nearest_fap_id": str(self.nearest_fap_id) if self.nearest_fap_id else None,
            "nearest_fap_name": self.nearest_fap_name,
            "distance_meters": float(self.distance_meters)
            if self.distance_meters is not None
            else None,
        }


def _nearest_fiber_access_point(
    db: Session, latitude: float, longitude: float
) -> tuple[FiberAccessPoint | None, float | None]:
    point = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)
    distance = func.ST_Distance(
        func.ST_Transform(FiberAccessPoint.geom, 3857),
        func.ST_Transform(point, 3857),
    ).label("distance_m")
    row = (
        db.query(FiberAccessPoint, distance)
        .filter(FiberAccessPoint.is_active.is_(True))
        .filter(FiberAccessPoint.geom.isnot(None))
        .order_by(FiberAccessPoint.geom.op("<->")(point))
        .first()
    )
    if row is None:
        return None, None
    fap, measured = row
    return fap, float(measured) if measured is not None else None


def assess(db: Session, *, query: FiberFeasibilityQuery) -> FiberFeasibilityResult:
    raw_radius = settings_spec.resolve_value(
        db, SettingDomain.projects, "selfserve_quote_feasibility_radius_meters"
    )
    try:
        radius = int(str(raw_radius))
    except (TypeError, ValueError):
        radius = 2000
    fap, distance = _nearest_fiber_access_point(
        db, float(query.latitude), float(query.longitude)
    )
    if fap is None or distance is None:
        return FiberFeasibilityResult(False, FiberFeasibilityStatus.out_of_area)
    status = (
        FiberFeasibilityStatus.covered
        if distance <= radius
        else FiberFeasibilityStatus.survey_required
    )
    return FiberFeasibilityResult(
        True, status, fap.id, fap.name, Decimal(str(round(distance, 1)))
    )
