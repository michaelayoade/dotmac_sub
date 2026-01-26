from __future__ import annotations

from datetime import datetime, timezone
import math

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.qualification import (
    BuildoutMilestone,
    BuildoutMilestoneStatus,
    BuildoutProject,
    BuildoutProjectStatus,
    BuildoutRequest,
    BuildoutRequestStatus,
    BuildoutUpdate,
    BuildoutStatus,
    CoverageArea,
    QualificationStatus,
    ServiceQualification,
)
from app.services.response import ListResponseMixin
from app.services.common import apply_ordering, apply_pagination, coerce_uuid, validate_enum
from app.models.subscriber import Address
from app.schemas.qualification import (
    BuildoutApproveRequest,
    BuildoutMilestoneCreate,
    BuildoutMilestoneUpdate,
    BuildoutProjectCreate,
    BuildoutProjectUpdate,
    BuildoutRequestCreate,
    BuildoutRequestUpdate,
    BuildoutUpdateCreate,
    CoverageAreaCreate,
    CoverageAreaUpdate,
    ServiceQualificationRequest,
)


def _extract_polygon(geometry: dict) -> list[tuple[float, float]]:
    if not isinstance(geometry, dict):
        raise HTTPException(status_code=400, detail="geometry_geojson must be an object")
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates")
    if geom_type == "Polygon":
        if not coords or not isinstance(coords, list):
            raise HTTPException(status_code=400, detail="Invalid Polygon coordinates")
        ring = coords[0]
    elif geom_type == "MultiPolygon":
        if not coords or not isinstance(coords, list):
            raise HTTPException(status_code=400, detail="Invalid MultiPolygon coordinates")
        ring = coords[0][0]
    else:
        raise HTTPException(status_code=400, detail="Geometry type must be Polygon")
    if not ring or len(ring) < 4:
        raise HTTPException(status_code=400, detail="Polygon ring must have 4+ points")
    points: list[tuple[float, float]] = []
    for coord in ring:
        if not isinstance(coord, (list, tuple)) or len(coord) < 2:
            raise HTTPException(status_code=400, detail="Invalid coordinate in polygon")
        lon, lat = coord[0], coord[1]
        points.append((float(lon), float(lat)))
    return points


def _polygon_bounds(points: list[tuple[float, float]]) -> dict:
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return {
        "min_longitude": min(lons),
        "max_longitude": max(lons),
        "min_latitude": min(lats),
        "max_latitude": max(lats),
    }


def _point_in_polygon(lon: float, lat: float, points: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(points) - 1
    for i in range(len(points)):
        xi, yi = points[i]
        xj, yj = points[j]
        intersect = ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi
        )
        if intersect:
            inside = not inside
        j = i
    return inside


def _centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    if not points:
        return 0.0, 0.0
    lon_sum = sum(p[0] for p in points)
    lat_sum = sum(p[1] for p in points)
    return lon_sum / len(points), lat_sum / len(points)


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius * c


class CoverageAreas(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: CoverageAreaCreate):
        polygon = _extract_polygon(payload.geometry_geojson)
        bounds = _polygon_bounds(polygon)
        data = payload.model_dump()
        data.update(bounds)
        area = CoverageArea(**data)
        db.add(area)
        db.commit()
        db.refresh(area)
        return area

    @staticmethod
    def get(db: Session, area_id: str):
        area = db.get(CoverageArea, area_id)
        if not area:
            raise HTTPException(status_code=404, detail="Coverage area not found")
        return area

    @staticmethod
    def list(
        db: Session,
        zone_key: str | None,
        buildout_status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(CoverageArea)
        if zone_key:
            query = query.filter(CoverageArea.zone_key == zone_key)
        if buildout_status:
            query = query.filter(
                CoverageArea.buildout_status
                == validate_enum(buildout_status, BuildoutStatus, "buildout_status")
            )
        if is_active is None:
            query = query.filter(CoverageArea.is_active.is_(True))
        else:
            query = query.filter(CoverageArea.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": CoverageArea.created_at,
                "name": CoverageArea.name,
                "priority": CoverageArea.priority,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, area_id: str, payload: CoverageAreaUpdate):
        area = db.get(CoverageArea, area_id)
        if not area:
            raise HTTPException(status_code=404, detail="Coverage area not found")
        data = payload.model_dump(exclude_unset=True)
        if "geometry_geojson" in data and data["geometry_geojson"] is not None:
            polygon = _extract_polygon(data["geometry_geojson"])
            data.update(_polygon_bounds(polygon))
        for key, value in data.items():
            setattr(area, key, value)
        db.commit()
        db.refresh(area)
        return area

    @staticmethod
    def delete(db: Session, area_id: str):
        area = db.get(CoverageArea, area_id)
        if not area:
            raise HTTPException(status_code=404, detail="Coverage area not found")
        area.is_active = False
        db.commit()


class ServiceQualifications(ListResponseMixin):
    @staticmethod
    def get(db: Session, qualification_id: str):
        qualification = db.get(ServiceQualification, qualification_id)
        if not qualification:
            raise HTTPException(status_code=404, detail="Service qualification not found")
        return qualification

    @staticmethod
    def list(
        db: Session,
        status: str | None,
        coverage_area_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ServiceQualification)
        if status:
            query = query.filter(
                ServiceQualification.status
                == validate_enum(status, QualificationStatus, "status")
            )
        if coverage_area_id:
            query = query.filter(ServiceQualification.coverage_area_id == coverage_area_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ServiceQualification.created_at, "status": ServiceQualification.status},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def check(db: Session, payload: ServiceQualificationRequest):
        lat = payload.latitude
        lon = payload.longitude
        if payload.address_id:
            address = db.get(Address, payload.address_id)
            if not address:
                raise HTTPException(status_code=404, detail="Address not found")
            if address.latitude is None or address.longitude is None:
                raise HTTPException(status_code=400, detail="Address missing coordinates")
            lat = address.latitude
            lon = address.longitude
        if lat is None or lon is None:
            raise HTTPException(status_code=400, detail="Latitude/longitude required")

        query = db.query(CoverageArea).filter(CoverageArea.is_active.is_(True))
        if payload.zone_key:
            query = query.filter(CoverageArea.zone_key == payload.zone_key)
        candidates = query.order_by(CoverageArea.priority.desc(), CoverageArea.created_at.desc()).all()

        matches: list[CoverageArea] = []
        for area in candidates:
            if (
                area.min_latitude is not None
                and area.max_latitude is not None
                and area.min_longitude is not None
                and area.max_longitude is not None
            ):
                if not (area.min_latitude <= lat <= area.max_latitude):
                    continue
                if not (area.min_longitude <= lon <= area.max_longitude):
                    continue
            polygon = _extract_polygon(area.geometry_geojson)
            if _point_in_polygon(lon, lat, polygon):
                matches.append(area)

        reasons: list[str] = []
        status = QualificationStatus.ineligible
        area = matches[0] if matches else None
        buildout_status = area.buildout_status if area else None
        estimated_install_window = area.buildout_window if area else None

        if not area:
            reasons.append("no_coverage")
        else:
            if not area.serviceable:
                reasons.append("not_serviceable")
            if area.buildout_status != BuildoutStatus.ready:
                reasons.append(f"buildout_status:{area.buildout_status.value}")
            constraints = area.constraints or {}
            allowed_tech = constraints.get("allowed_tech")
            if allowed_tech and payload.requested_tech:
                if payload.requested_tech not in allowed_tech:
                    reasons.append("tech_not_supported")
            capacity_available = constraints.get("capacity_available")
            if capacity_available is False:
                reasons.append("capacity_unavailable")
            max_distance_km = constraints.get("max_distance_km")
            if max_distance_km is not None:
                centroid_lon, centroid_lat = _centroid(_extract_polygon(area.geometry_geojson))
                distance = _haversine_km(lon, lat, centroid_lon, centroid_lat)
                if distance > float(max_distance_km):
                    reasons.append("distance_exceeds")

            if not reasons:
                status = QualificationStatus.eligible
            elif (
                area.buildout_status != BuildoutStatus.ready
                and all(reason.startswith("buildout_status") for reason in reasons)
            ):
                status = QualificationStatus.needs_buildout

        qualification = ServiceQualification(
            coverage_area_id=area.id if area else None,
            address_id=payload.address_id,
            latitude=lat,
            longitude=lon,
            requested_tech=payload.requested_tech,
            status=status,
            buildout_status=buildout_status,
            estimated_install_window=estimated_install_window,
            reasons=reasons,
            metadata=payload.metadata_,
            created_at=datetime.now(timezone.utc),
        )
        db.add(qualification)
        db.commit()
        db.refresh(qualification)
        if (
            status == QualificationStatus.needs_buildout
            and qualification.coverage_area_id
            and (qualification.address_id or payload.address_id)
        ):
            if buildout_status not in {
                BuildoutStatus.planned,
                BuildoutStatus.in_progress,
            }:
                return qualification
            existing_request = (
                db.query(BuildoutRequest)
                .filter(BuildoutRequest.coverage_area_id == qualification.coverage_area_id)
                .filter(BuildoutRequest.address_id == qualification.address_id)
                .filter(
                    BuildoutRequest.status.in_(
                        [
                            BuildoutRequestStatus.submitted,
                            BuildoutRequestStatus.approved,
                        ]
                    )
                )
                .first()
            )
            if not existing_request:
                request = BuildoutRequest(
                    qualification_id=qualification.id,
                    coverage_area_id=qualification.coverage_area_id,
                    address_id=qualification.address_id,
                    requested_by="system",
                    status=BuildoutRequestStatus.submitted,
                    notes="Auto-created from qualification check",
                )
                db.add(request)
                db.commit()
                db.refresh(qualification)
        return qualification


class BuildoutRequests(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: BuildoutRequestCreate):
        data = payload.model_dump()
        if payload.qualification_id:
            qualification = db.get(ServiceQualification, payload.qualification_id)
            if not qualification:
                raise HTTPException(status_code=404, detail="Qualification not found")
            data.setdefault("coverage_area_id", qualification.coverage_area_id)
            data.setdefault("address_id", qualification.address_id)
            qualification.status = QualificationStatus.needs_buildout
            if qualification.buildout_status is None:
                qualification.buildout_status = BuildoutStatus.planned
        request = BuildoutRequest(**data)
        db.add(request)
        db.commit()
        db.refresh(request)
        return request

    @staticmethod
    def get(db: Session, request_id: str):
        request = db.get(BuildoutRequest, request_id)
        if not request:
            raise HTTPException(status_code=404, detail="Buildout request not found")
        return request

    @staticmethod
    def list(
        db: Session,
        status: str | None,
        coverage_area_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(BuildoutRequest)
        if status:
            query = query.filter(
                BuildoutRequest.status
                == validate_enum(status, BuildoutRequestStatus, "status")
            )
        if coverage_area_id:
            query = query.filter(BuildoutRequest.coverage_area_id == coverage_area_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": BuildoutRequest.created_at, "status": BuildoutRequest.status},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, request_id: str, payload: BuildoutRequestUpdate):
        request = db.get(BuildoutRequest, request_id)
        if not request:
            raise HTTPException(status_code=404, detail="Buildout request not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(request, key, value)
        db.commit()
        db.refresh(request)
        return request

    @staticmethod
    def approve(db: Session, request_id: str, payload: BuildoutApproveRequest):
        request = db.get(BuildoutRequest, request_id)
        if not request:
            raise HTTPException(status_code=404, detail="Buildout request not found")
        if request.status == BuildoutRequestStatus.approved and request.project:
            return request.project
        request.status = BuildoutRequestStatus.approved
        project = BuildoutProject(
            request_id=request.id,
            coverage_area_id=request.coverage_area_id,
            address_id=request.address_id,
            status=BuildoutProjectStatus.planned,
            progress_percent=0,
            target_ready_date=payload.target_ready_date,
            notes=payload.notes,
        )
        db.add(project)
        db.flush()
        update = BuildoutUpdate(
            project_id=project.id,
            status=BuildoutProjectStatus.planned,
            message="Buildout approved",
        )
        db.add(update)
        db.commit()
        db.refresh(project)
        return project


class BuildoutProjects(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: BuildoutProjectCreate):
        project = BuildoutProject(**payload.model_dump())
        db.add(project)
        db.commit()
        db.refresh(project)
        return project

    @staticmethod
    def get(db: Session, project_id: str):
        project = db.get(BuildoutProject, project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Buildout project not found")
        return project

    @staticmethod
    def list(
        db: Session,
        status: str | None,
        coverage_area_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(BuildoutProject)
        if status:
            query = query.filter(
                BuildoutProject.status
                == validate_enum(status, BuildoutProjectStatus, "status")
            )
        if coverage_area_id:
            query = query.filter(BuildoutProject.coverage_area_id == coverage_area_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": BuildoutProject.created_at, "status": BuildoutProject.status},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, project_id: str, payload: BuildoutProjectUpdate):
        project = db.get(BuildoutProject, project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Buildout project not found")
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(project, key, value)
        if "status" in data or "progress_percent" in data or "notes" in data:
            update = BuildoutUpdate(
                project_id=project.id,
                status=project.status,
                message=data.get("notes") or "Project updated",
            )
            db.add(update)
        db.commit()
        db.refresh(project)
        return project

    @staticmethod
    def delete(db: Session, project_id: str):
        project = db.get(BuildoutProject, project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Buildout project not found")
        db.delete(project)
        db.commit()


class BuildoutMilestones(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: BuildoutMilestoneCreate):
        milestone = BuildoutMilestone(**payload.model_dump())
        db.add(milestone)
        db.commit()
        db.refresh(milestone)
        return milestone

    @staticmethod
    def get(db: Session, milestone_id: str):
        milestone = db.get(BuildoutMilestone, milestone_id)
        if not milestone:
            raise HTTPException(status_code=404, detail="Buildout milestone not found")
        return milestone

    @staticmethod
    def list(
        db: Session,
        project_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(BuildoutMilestone)
        if project_id:
            query = query.filter(BuildoutMilestone.project_id == project_id)
        if status:
            query = query.filter(
                BuildoutMilestone.status
                == validate_enum(status, BuildoutMilestoneStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": BuildoutMilestone.created_at, "order_index": BuildoutMilestone.order_index},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, milestone_id: str, payload: BuildoutMilestoneUpdate):
        milestone = db.get(BuildoutMilestone, milestone_id)
        if not milestone:
            raise HTTPException(status_code=404, detail="Buildout milestone not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(milestone, key, value)
        db.commit()
        db.refresh(milestone)
        return milestone

    @staticmethod
    def delete(db: Session, milestone_id: str):
        milestone = db.get(BuildoutMilestone, milestone_id)
        if not milestone:
            raise HTTPException(status_code=404, detail="Buildout milestone not found")
        db.delete(milestone)
        db.commit()


class BuildoutUpdates(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: BuildoutUpdateCreate):
        update = BuildoutUpdate(**payload.model_dump())
        db.add(update)
        db.commit()
        db.refresh(update)
        return update

    @staticmethod
    def list(
        db: Session,
        project_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(BuildoutUpdate)
        if project_id:
            query = query.filter(BuildoutUpdate.project_id == project_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": BuildoutUpdate.created_at},
        )
        return apply_pagination(query, limit, offset).all()


coverage_areas = CoverageAreas()
service_qualifications = ServiceQualifications()
buildout_requests = BuildoutRequests()
buildout_projects = BuildoutProjects()
buildout_milestones = BuildoutMilestones()
buildout_updates = BuildoutUpdates()
