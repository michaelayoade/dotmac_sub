"""Tests for qualification service."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.models.qualification import (
    BuildoutMilestone,
    BuildoutMilestoneStatus,
    BuildoutProject,
    BuildoutProjectStatus,
    BuildoutRequest,
    BuildoutRequestStatus,
    BuildoutStatus,
    BuildoutUpdate,
    CoverageArea,
    QualificationStatus,
    ServiceQualification,
)
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
from app.services import qualification as qualification_service
from app.services.common import apply_ordering, apply_pagination, validate_enum

# Valid polygon for testing (simple square)
VALID_POLYGON = {
    "type": "Polygon",
    "coordinates": [
        [
            [-122.5, 37.5],
            [-122.5, 38.0],
            [-122.0, 38.0],
            [-122.0, 37.5],
            [-122.5, 37.5],
        ]
    ],
}

# Point inside the polygon
POINT_INSIDE = (-122.25, 37.75)  # lon, lat

# Point outside the polygon
POINT_OUTSIDE = (-121.0, 39.0)  # lon, lat


class TestApplyOrdering:
    """Tests for _apply_ordering helper."""

    def test_orders_ascending(self, db_session):
        """Test orders ascending."""
        area1 = CoverageArea(name="Beta", geometry_geojson=VALID_POLYGON)
        area2 = CoverageArea(name="Alpha", geometry_geojson=VALID_POLYGON)
        db_session.add_all([area1, area2])
        db_session.commit()

        query = db_session.query(CoverageArea)
        result = apply_ordering(
            query, "name", "asc", {"name": CoverageArea.name}
        )
        items = result.all()
        assert items[0].name == "Alpha"
        assert items[1].name == "Beta"

    def test_orders_descending(self, db_session):
        """Test orders descending."""
        area1 = CoverageArea(name="Alpha", geometry_geojson=VALID_POLYGON)
        area2 = CoverageArea(name="Beta", geometry_geojson=VALID_POLYGON)
        db_session.add_all([area1, area2])
        db_session.commit()

        query = db_session.query(CoverageArea)
        result = apply_ordering(
            query, "name", "desc", {"name": CoverageArea.name}
        )
        items = result.all()
        assert items[0].name == "Beta"

    def test_raises_for_invalid_column(self, db_session):
        """Test raises for invalid order_by column."""
        query = db_session.query(CoverageArea)
        with pytest.raises(HTTPException) as exc_info:
            apply_ordering(
                query, "invalid", "asc", {"name": CoverageArea.name}
            )
        assert exc_info.value.status_code == 400


class TestApplyPagination:
    """Tests for _apply_pagination helper."""

    def test_applies_limit_and_offset(self, db_session):
        """Test applies limit and offset."""
        for i in range(5):
            db_session.add(CoverageArea(name=f"Area{i}", geometry_geojson=VALID_POLYGON))
        db_session.commit()

        query = db_session.query(CoverageArea).order_by(CoverageArea.name)
        result = apply_pagination(query, limit=2, offset=1)
        items = result.all()
        assert len(items) == 2
        assert items[0].name == "Area1"


class TestValidateEnum:
    """Tests for _validate_enum helper."""

    def test_validates_valid_enum_value(self):
        """Test validates valid enum value."""
        result = validate_enum("ready", BuildoutStatus, "status")
        assert result == BuildoutStatus.ready

    def test_returns_none_for_none_value(self):
        """Test returns None for None value."""
        result = validate_enum(None, BuildoutStatus, "status")
        assert result is None

    def test_raises_for_invalid_enum_value(self):
        """Test raises for invalid enum value."""
        with pytest.raises(HTTPException) as exc_info:
            validate_enum("invalid", BuildoutStatus, "status")
        assert exc_info.value.status_code == 400


class TestExtractPolygon:
    """Tests for _extract_polygon helper."""

    def test_extracts_polygon_points(self):
        """Test extracts polygon points."""
        result = qualification_service._extract_polygon(VALID_POLYGON)
        assert len(result) == 5
        assert result[0] == (-122.5, 37.5)

    def test_extracts_multipolygon_points(self):
        """Test extracts MultiPolygon points."""
        multipolygon = {
            "type": "MultiPolygon",
            "coordinates": [VALID_POLYGON["coordinates"]],
        }
        result = qualification_service._extract_polygon(multipolygon)
        assert len(result) == 5

    def test_raises_for_non_dict_geometry(self):
        """Test raises for non-dict geometry."""
        with pytest.raises(HTTPException) as exc_info:
            qualification_service._extract_polygon("not a dict")
        assert exc_info.value.status_code == 400
        assert "must be an object" in str(exc_info.value.detail)

    def test_raises_for_invalid_polygon_coordinates(self):
        """Test raises for invalid Polygon coordinates."""
        with pytest.raises(HTTPException) as exc_info:
            qualification_service._extract_polygon({"type": "Polygon", "coordinates": None})
        assert exc_info.value.status_code == 400

    def test_raises_for_invalid_multipolygon_coordinates(self):
        """Test raises for invalid MultiPolygon coordinates."""
        with pytest.raises(HTTPException) as exc_info:
            qualification_service._extract_polygon({"type": "MultiPolygon", "coordinates": None})
        assert exc_info.value.status_code == 400

    def test_raises_for_unsupported_geometry_type(self):
        """Test raises for unsupported geometry type."""
        with pytest.raises(HTTPException) as exc_info:
            qualification_service._extract_polygon({"type": "Point", "coordinates": [0, 0]})
        assert exc_info.value.status_code == 400
        assert "Polygon" in str(exc_info.value.detail)

    def test_raises_for_polygon_with_too_few_points(self):
        """Test raises for polygon ring with less than 4 points."""
        with pytest.raises(HTTPException) as exc_info:
            qualification_service._extract_polygon({
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 1], [0, 0]]],
            })
        assert exc_info.value.status_code == 400
        assert "4+" in str(exc_info.value.detail)

    def test_raises_for_invalid_coordinate_format(self):
        """Test raises for invalid coordinate format."""
        with pytest.raises(HTTPException) as exc_info:
            qualification_service._extract_polygon({
                "type": "Polygon",
                "coordinates": [[0, 1, 2, 3, 4]],  # Not a list of [lon, lat] pairs
            })
        assert exc_info.value.status_code == 400


class TestPolygonBounds:
    """Tests for _polygon_bounds helper."""

    def test_calculates_bounds(self):
        """Test calculates bounds correctly."""
        points = [(-122.5, 37.5), (-122.5, 38.0), (-122.0, 38.0), (-122.0, 37.5)]
        result = qualification_service._polygon_bounds(points)
        assert result["min_longitude"] == -122.5
        assert result["max_longitude"] == -122.0
        assert result["min_latitude"] == 37.5
        assert result["max_latitude"] == 38.0


class TestPointInPolygon:
    """Tests for _point_in_polygon helper."""

    def test_detects_point_inside(self):
        """Test detects point inside polygon."""
        points = [(-122.5, 37.5), (-122.5, 38.0), (-122.0, 38.0), (-122.0, 37.5), (-122.5, 37.5)]
        result = qualification_service._point_in_polygon(-122.25, 37.75, points)
        assert result is True

    def test_detects_point_outside(self):
        """Test detects point outside polygon."""
        points = [(-122.5, 37.5), (-122.5, 38.0), (-122.0, 38.0), (-122.0, 37.5), (-122.5, 37.5)]
        result = qualification_service._point_in_polygon(-121.0, 39.0, points)
        assert result is False


class TestCentroid:
    """Tests for _centroid helper."""

    def test_calculates_centroid(self):
        """Test calculates centroid."""
        points = [(-122.5, 37.5), (-122.5, 38.0), (-122.0, 38.0), (-122.0, 37.5)]
        lon, lat = qualification_service._centroid(points)
        assert lon == -122.25
        assert lat == 37.75

    def test_returns_zero_for_empty_points(self):
        """Test returns (0, 0) for empty points list."""
        lon, lat = qualification_service._centroid([])
        assert lon == 0.0
        assert lat == 0.0


class TestHaversineKm:
    """Tests for _haversine_km helper."""

    def test_calculates_distance(self):
        """Test calculates distance between two points."""
        # San Francisco to Los Angeles ~ 559 km
        distance = qualification_service._haversine_km(-122.4194, 37.7749, -118.2437, 34.0522)
        assert 550 < distance < 570

    def test_returns_zero_for_same_point(self):
        """Test returns 0 for same point."""
        distance = qualification_service._haversine_km(-122.0, 37.0, -122.0, 37.0)
        assert distance == 0.0


# ============ CoverageAreas Tests ============


class TestCoverageAreasCreate:
    """Tests for CoverageAreas.create."""

    def test_creates_coverage_area(self, db_session):
        """Test creates coverage area."""
        payload = CoverageAreaCreate(
            name="Test Area",
            geometry_geojson=VALID_POLYGON,
        )
        result = qualification_service.coverage_areas.create(db_session, payload)
        assert result.id is not None
        assert result.name == "Test Area"
        assert result.min_latitude is not None
        assert result.max_latitude is not None

    def test_creates_with_all_fields(self, db_session):
        """Test creates with all fields."""
        payload = CoverageAreaCreate(
            name="Full Area",
            code="FULL",
            zone_key="zone1",
            buildout_status=BuildoutStatus.ready,
            buildout_window="Q1 2026",
            serviceable=True,
            priority=10,
            geometry_geojson=VALID_POLYGON,
            constraints={"max_distance_km": 5.0},
        )
        result = qualification_service.coverage_areas.create(db_session, payload)
        assert result.code == "FULL"
        assert result.zone_key == "zone1"
        assert result.buildout_status == BuildoutStatus.ready
        assert result.priority == 10


class TestCoverageAreasGet:
    """Tests for CoverageAreas.get."""

    def test_gets_coverage_area(self, db_session):
        """Test gets coverage area."""
        area = CoverageArea(name="Test", geometry_geojson=VALID_POLYGON)
        db_session.add(area)
        db_session.commit()

        result = qualification_service.coverage_areas.get(db_session, str(area.id))
        assert result.id == area.id

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.coverage_areas.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


class TestCoverageAreasList:
    """Tests for CoverageAreas.list."""

    def test_lists_active_areas_by_default(self, db_session):
        """Test lists only active areas by default."""
        active = CoverageArea(name="Active", geometry_geojson=VALID_POLYGON, is_active=True)
        inactive = CoverageArea(name="Inactive", geometry_geojson=VALID_POLYGON, is_active=False)
        db_session.add_all([active, inactive])
        db_session.commit()

        result = qualification_service.coverage_areas.list(
            db_session,
            zone_key=None,
            buildout_status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(result) == 1
        assert result[0].name == "Active"

    def test_filters_by_zone_key(self, db_session):
        """Test filters by zone_key."""
        area1 = CoverageArea(name="Zone1", zone_key="z1", geometry_geojson=VALID_POLYGON)
        area2 = CoverageArea(name="Zone2", zone_key="z2", geometry_geojson=VALID_POLYGON)
        db_session.add_all([area1, area2])
        db_session.commit()

        result = qualification_service.coverage_areas.list(
            db_session,
            zone_key="z1",
            buildout_status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(result) == 1
        assert result[0].zone_key == "z1"

    def test_filters_by_buildout_status(self, db_session):
        """Test filters by buildout_status."""
        ready = CoverageArea(name="Ready", buildout_status=BuildoutStatus.ready, geometry_geojson=VALID_POLYGON)
        planned = CoverageArea(name="Planned", buildout_status=BuildoutStatus.planned, geometry_geojson=VALID_POLYGON)
        db_session.add_all([ready, planned])
        db_session.commit()

        result = qualification_service.coverage_areas.list(
            db_session,
            zone_key=None,
            buildout_status="ready",
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(result) == 1
        assert result[0].buildout_status == BuildoutStatus.ready

    def test_filters_by_is_active_explicit(self, db_session):
        """Test filters by explicit is_active value."""
        active = CoverageArea(name="Active", geometry_geojson=VALID_POLYGON, is_active=True)
        inactive = CoverageArea(name="Inactive", geometry_geojson=VALID_POLYGON, is_active=False)
        db_session.add_all([active, inactive])
        db_session.commit()

        result = qualification_service.coverage_areas.list(
            db_session,
            zone_key=None,
            buildout_status=None,
            is_active=False,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(result) == 1
        assert result[0].name == "Inactive"


class TestCoverageAreasUpdate:
    """Tests for CoverageAreas.update."""

    def test_updates_coverage_area(self, db_session):
        """Test updates coverage area."""
        area = CoverageArea(name="Original", geometry_geojson=VALID_POLYGON)
        db_session.add(area)
        db_session.commit()

        payload = CoverageAreaUpdate(name="Updated")
        result = qualification_service.coverage_areas.update(db_session, str(area.id), payload)
        assert result.name == "Updated"

    def test_updates_geometry_recalculates_bounds(self, db_session):
        """Test updating geometry recalculates bounds."""
        area = CoverageArea(
            name="Test",
            geometry_geojson=VALID_POLYGON,
            min_latitude=37.5,
            max_latitude=38.0,
        )
        db_session.add(area)
        db_session.commit()

        new_polygon = {
            "type": "Polygon",
            "coordinates": [
                [
                    [-123.0, 36.0],
                    [-123.0, 37.0],
                    [-122.0, 37.0],
                    [-122.0, 36.0],
                    [-123.0, 36.0],
                ]
            ],
        }
        payload = CoverageAreaUpdate(geometry_geojson=new_polygon)
        result = qualification_service.coverage_areas.update(db_session, str(area.id), payload)
        assert result.min_latitude == 36.0
        assert result.max_latitude == 37.0

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        payload = CoverageAreaUpdate(name="Test")
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.coverage_areas.update(db_session, str(uuid.uuid4()), payload)
        assert exc_info.value.status_code == 404


class TestCoverageAreasDelete:
    """Tests for CoverageAreas.delete."""

    def test_soft_deletes_coverage_area(self, db_session):
        """Test soft deletes coverage area."""
        area = CoverageArea(name="Test", geometry_geojson=VALID_POLYGON, is_active=True)
        db_session.add(area)
        db_session.commit()

        qualification_service.coverage_areas.delete(db_session, str(area.id))
        db_session.refresh(area)
        assert area.is_active is False

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.coverage_areas.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============ ServiceQualifications Tests ============


class TestServiceQualificationsGet:
    """Tests for ServiceQualifications.get."""

    def test_gets_qualification(self, db_session):
        """Test gets qualification."""
        qual = ServiceQualification(latitude=37.75, longitude=-122.25)
        db_session.add(qual)
        db_session.commit()

        result = qualification_service.service_qualifications.get(db_session, str(qual.id))
        assert result.id == qual.id

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.service_qualifications.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


class TestServiceQualificationsList:
    """Tests for ServiceQualifications.list."""

    def test_lists_qualifications(self, db_session):
        """Test lists qualifications."""
        q1 = ServiceQualification(latitude=37.75, longitude=-122.25)
        q2 = ServiceQualification(latitude=37.80, longitude=-122.30)
        db_session.add_all([q1, q2])
        db_session.commit()

        result = qualification_service.service_qualifications.list(
            db_session,
            status=None,
            coverage_area_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(result) == 2

    def test_filters_by_status(self, db_session):
        """Test filters by status."""
        eligible = ServiceQualification(latitude=37.75, longitude=-122.25, status=QualificationStatus.eligible)
        ineligible = ServiceQualification(latitude=37.80, longitude=-122.30, status=QualificationStatus.ineligible)
        db_session.add_all([eligible, ineligible])
        db_session.commit()

        result = qualification_service.service_qualifications.list(
            db_session,
            status="eligible",
            coverage_area_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(result) == 1
        assert result[0].status == QualificationStatus.eligible

    def test_filters_by_coverage_area_id(self, db_session):
        """Test filters by coverage_area_id."""
        area = CoverageArea(name="Test", geometry_geojson=VALID_POLYGON)
        db_session.add(area)
        db_session.commit()

        q1 = ServiceQualification(latitude=37.75, longitude=-122.25, coverage_area_id=area.id)
        q2 = ServiceQualification(latitude=37.80, longitude=-122.30)
        db_session.add_all([q1, q2])
        db_session.commit()

        result = qualification_service.service_qualifications.list(
            db_session,
            status=None,
            coverage_area_id=str(area.id),
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(result) == 1
        assert result[0].coverage_area_id == area.id


class TestServiceQualificationsCheck:
    """Tests for ServiceQualifications.check."""

    def test_returns_eligible_for_point_in_ready_area(self, db_session):
        """Test returns eligible for point in ready serviceable area."""
        area = CoverageArea(
            name="Ready Area",
            geometry_geojson=VALID_POLYGON,
            buildout_status=BuildoutStatus.ready,
            serviceable=True,
            min_latitude=37.5,
            max_latitude=38.0,
            min_longitude=-122.5,
            max_longitude=-122.0,
        )
        db_session.add(area)
        db_session.commit()

        payload = ServiceQualificationRequest(
            latitude=POINT_INSIDE[1],
            longitude=POINT_INSIDE[0],
        )
        result = qualification_service.service_qualifications.check(db_session, payload)
        assert result.status == QualificationStatus.eligible
        assert result.coverage_area_id == area.id

    def test_returns_ineligible_for_no_coverage(self, db_session):
        """Test returns ineligible for point outside all coverage areas."""
        payload = ServiceQualificationRequest(
            latitude=POINT_OUTSIDE[1],
            longitude=POINT_OUTSIDE[0],
        )
        result = qualification_service.service_qualifications.check(db_session, payload)
        assert result.status == QualificationStatus.ineligible
        assert "no_coverage" in result.reasons

    def test_returns_ineligible_for_not_serviceable(self, db_session):
        """Test returns ineligible for not serviceable area."""
        area = CoverageArea(
            name="Not Serviceable",
            geometry_geojson=VALID_POLYGON,
            buildout_status=BuildoutStatus.ready,
            serviceable=False,
            min_latitude=37.5,
            max_latitude=38.0,
            min_longitude=-122.5,
            max_longitude=-122.0,
        )
        db_session.add(area)
        db_session.commit()

        payload = ServiceQualificationRequest(
            latitude=POINT_INSIDE[1],
            longitude=POINT_INSIDE[0],
        )
        result = qualification_service.service_qualifications.check(db_session, payload)
        assert result.status == QualificationStatus.ineligible
        assert "not_serviceable" in result.reasons

    def test_returns_needs_buildout_for_planned_area(self, db_session):
        """Test returns needs_buildout for planned area."""
        area = CoverageArea(
            name="Planned Area",
            geometry_geojson=VALID_POLYGON,
            buildout_status=BuildoutStatus.planned,
            serviceable=True,
            min_latitude=37.5,
            max_latitude=38.0,
            min_longitude=-122.5,
            max_longitude=-122.0,
        )
        db_session.add(area)
        db_session.commit()

        payload = ServiceQualificationRequest(
            latitude=POINT_INSIDE[1],
            longitude=POINT_INSIDE[0],
        )
        result = qualification_service.service_qualifications.check(db_session, payload)
        assert result.status == QualificationStatus.needs_buildout
        assert any("buildout_status" in r for r in result.reasons)

    def test_uses_address_coordinates(self, db_session, subscriber):
        """Test uses address coordinates when provided."""
        area = CoverageArea(
            name="Test",
            geometry_geojson=VALID_POLYGON,
            buildout_status=BuildoutStatus.ready,
            serviceable=True,
            min_latitude=37.5,
            max_latitude=38.0,
            min_longitude=-122.5,
            max_longitude=-122.0,
        )
        db_session.add(area)
        db_session.commit()

        address = Address(
            subscriber_id=subscriber.id,
            address_line1="123 Test St",
            city="Test",
            region="CA",
            postal_code="94102",
            latitude=POINT_INSIDE[1],
            longitude=POINT_INSIDE[0],
        )
        db_session.add(address)
        db_session.commit()

        payload = ServiceQualificationRequest(address_id=address.id)
        result = qualification_service.service_qualifications.check(db_session, payload)
        assert result.status == QualificationStatus.eligible

    def test_raises_for_address_not_found(self, db_session):
        """Test raises for address not found."""
        payload = ServiceQualificationRequest(address_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.service_qualifications.check(db_session, payload)
        assert exc_info.value.status_code == 404

    def test_raises_for_address_missing_coordinates(self, db_session, subscriber):
        """Test raises for address missing coordinates."""
        address = Address(
            subscriber_id=subscriber.id,
            address_line1="123 Test St",
            city="Test",
            region="CA",
            postal_code="94102",
        )
        db_session.add(address)
        db_session.commit()

        payload = ServiceQualificationRequest(address_id=address.id)
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.service_qualifications.check(db_session, payload)
        assert exc_info.value.status_code == 400
        assert "coordinates" in str(exc_info.value.detail)

    def test_raises_for_missing_latitude_longitude(self, db_session):
        """Test raises when no coordinates provided."""
        payload = ServiceQualificationRequest()
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.service_qualifications.check(db_session, payload)
        assert exc_info.value.status_code == 400

    def test_filters_by_zone_key(self, db_session):
        """Test filters coverage areas by zone_key."""
        area1 = CoverageArea(
            name="Zone1",
            zone_key="z1",
            geometry_geojson=VALID_POLYGON,
            buildout_status=BuildoutStatus.ready,
            serviceable=True,
            min_latitude=37.5,
            max_latitude=38.0,
            min_longitude=-122.5,
            max_longitude=-122.0,
        )
        area2 = CoverageArea(
            name="Zone2",
            zone_key="z2",
            geometry_geojson=VALID_POLYGON,
            buildout_status=BuildoutStatus.ready,
            serviceable=True,
            min_latitude=37.5,
            max_latitude=38.0,
            min_longitude=-122.5,
            max_longitude=-122.0,
        )
        db_session.add_all([area1, area2])
        db_session.commit()

        payload = ServiceQualificationRequest(
            latitude=POINT_INSIDE[1],
            longitude=POINT_INSIDE[0],
            zone_key="z1",
        )
        result = qualification_service.service_qualifications.check(db_session, payload)
        assert result.coverage_area_id == area1.id

    def test_checks_tech_constraints(self, db_session):
        """Test checks technology constraints."""
        area = CoverageArea(
            name="Fiber Only",
            geometry_geojson=VALID_POLYGON,
            buildout_status=BuildoutStatus.ready,
            serviceable=True,
            constraints={"allowed_tech": ["fiber"]},
            min_latitude=37.5,
            max_latitude=38.0,
            min_longitude=-122.5,
            max_longitude=-122.0,
        )
        db_session.add(area)
        db_session.commit()

        payload = ServiceQualificationRequest(
            latitude=POINT_INSIDE[1],
            longitude=POINT_INSIDE[0],
            requested_tech="wireless",
        )
        result = qualification_service.service_qualifications.check(db_session, payload)
        assert result.status == QualificationStatus.ineligible
        assert "tech_not_supported" in result.reasons

    def test_checks_capacity_constraints(self, db_session):
        """Test checks capacity constraints."""
        area = CoverageArea(
            name="No Capacity",
            geometry_geojson=VALID_POLYGON,
            buildout_status=BuildoutStatus.ready,
            serviceable=True,
            constraints={"capacity_available": False},
            min_latitude=37.5,
            max_latitude=38.0,
            min_longitude=-122.5,
            max_longitude=-122.0,
        )
        db_session.add(area)
        db_session.commit()

        payload = ServiceQualificationRequest(
            latitude=POINT_INSIDE[1],
            longitude=POINT_INSIDE[0],
        )
        result = qualification_service.service_qualifications.check(db_session, payload)
        assert "capacity_unavailable" in result.reasons

    def test_checks_distance_constraints(self, db_session):
        """Test checks max distance constraints."""
        # Create a small polygon so point is far from centroid
        small_polygon = {
            "type": "Polygon",
            "coordinates": [
                [
                    [-122.5, 37.5],
                    [-122.5, 38.0],
                    [-122.0, 38.0],
                    [-122.0, 37.5],
                    [-122.5, 37.5],
                ]
            ],
        }
        area = CoverageArea(
            name="Distance Limited",
            geometry_geojson=small_polygon,
            buildout_status=BuildoutStatus.ready,
            serviceable=True,
            constraints={"max_distance_km": 0.001},  # Very small distance
            min_latitude=37.5,
            max_latitude=38.0,
            min_longitude=-122.5,
            max_longitude=-122.0,
        )
        db_session.add(area)
        db_session.commit()

        payload = ServiceQualificationRequest(
            latitude=37.51,  # Near edge, not center
            longitude=-122.49,
        )
        result = qualification_service.service_qualifications.check(db_session, payload)
        assert "distance_exceeds" in result.reasons

    def test_skips_area_outside_latitude_bounds(self, db_session):
        """Test skips areas where point is outside latitude bounds."""
        area = CoverageArea(
            name="Test",
            geometry_geojson=VALID_POLYGON,
            buildout_status=BuildoutStatus.ready,
            serviceable=True,
            min_latitude=37.5,
            max_latitude=38.0,
            min_longitude=-122.5,
            max_longitude=-122.0,
        )
        db_session.add(area)
        db_session.commit()

        # Point outside latitude bounds
        payload = ServiceQualificationRequest(
            latitude=40.0,
            longitude=-122.25,
        )
        result = qualification_service.service_qualifications.check(db_session, payload)
        assert result.status == QualificationStatus.ineligible
        assert "no_coverage" in result.reasons

    def test_skips_area_outside_longitude_bounds(self, db_session):
        """Test skips areas where point is outside longitude bounds."""
        area = CoverageArea(
            name="Test",
            geometry_geojson=VALID_POLYGON,
            buildout_status=BuildoutStatus.ready,
            serviceable=True,
            min_latitude=37.5,
            max_latitude=38.0,
            min_longitude=-122.5,
            max_longitude=-122.0,
        )
        db_session.add(area)
        db_session.commit()

        # Point outside longitude bounds (latitude is within bounds)
        payload = ServiceQualificationRequest(
            latitude=37.75,
            longitude=-120.0,
        )
        result = qualification_service.service_qualifications.check(db_session, payload)
        assert result.status == QualificationStatus.ineligible
        assert "no_coverage" in result.reasons

    def test_creates_buildout_request_for_planned_area(self, db_session, subscriber):
        """Test creates buildout request for planned area with address."""
        area = CoverageArea(
            name="Planned Area",
            geometry_geojson=VALID_POLYGON,
            buildout_status=BuildoutStatus.planned,
            serviceable=True,
            min_latitude=37.5,
            max_latitude=38.0,
            min_longitude=-122.5,
            max_longitude=-122.0,
        )
        db_session.add(area)
        db_session.commit()

        address = Address(
            subscriber_id=subscriber.id,
            address_line1="123 Test St",
            city="Test",
            region="CA",
            postal_code="94102",
            latitude=POINT_INSIDE[1],
            longitude=POINT_INSIDE[0],
        )
        db_session.add(address)
        db_session.commit()

        payload = ServiceQualificationRequest(address_id=address.id)
        result = qualification_service.service_qualifications.check(db_session, payload)
        assert result.status == QualificationStatus.needs_buildout

        # Check buildout request was created
        requests = db_session.query(BuildoutRequest).all()
        assert len(requests) == 1
        assert requests[0].coverage_area_id == area.id

    def test_does_not_duplicate_buildout_request(self, db_session, subscriber):
        """Test does not create duplicate buildout request."""
        area = CoverageArea(
            name="Planned Area",
            geometry_geojson=VALID_POLYGON,
            buildout_status=BuildoutStatus.planned,
            serviceable=True,
            min_latitude=37.5,
            max_latitude=38.0,
            min_longitude=-122.5,
            max_longitude=-122.0,
        )
        db_session.add(area)
        db_session.commit()

        address = Address(
            subscriber_id=subscriber.id,
            address_line1="123 Test St",
            city="Test",
            region="CA",
            postal_code="94102",
            latitude=POINT_INSIDE[1],
            longitude=POINT_INSIDE[0],
        )
        db_session.add(address)
        db_session.commit()

        # Create existing request
        existing_request = BuildoutRequest(
            coverage_area_id=area.id,
            address_id=address.id,
            status=BuildoutRequestStatus.submitted,
        )
        db_session.add(existing_request)
        db_session.commit()

        payload = ServiceQualificationRequest(address_id=address.id)
        qualification_service.service_qualifications.check(db_session, payload)

        # Should still only have 1 request
        requests = db_session.query(BuildoutRequest).all()
        assert len(requests) == 1

    def test_does_not_create_buildout_request_for_not_planned_status(self, db_session, subscriber):
        """Test does not create buildout request when buildout_status is not_planned."""
        area = CoverageArea(
            name="Not Planned Area",
            geometry_geojson=VALID_POLYGON,
            buildout_status=BuildoutStatus.not_planned,
            serviceable=True,
            min_latitude=37.5,
            max_latitude=38.0,
            min_longitude=-122.5,
            max_longitude=-122.0,
        )
        db_session.add(area)
        db_session.commit()

        address = Address(
            subscriber_id=subscriber.id,
            address_line1="456 Test St",
            city="Test",
            region="CA",
            postal_code="94102",
            latitude=POINT_INSIDE[1],
            longitude=POINT_INSIDE[0],
        )
        db_session.add(address)
        db_session.commit()

        payload = ServiceQualificationRequest(address_id=address.id)
        result = qualification_service.service_qualifications.check(db_session, payload)

        # Should return needs_buildout but NOT create a request since status is not_planned
        assert result.status == QualificationStatus.needs_buildout

        # No buildout request should be created
        requests = db_session.query(BuildoutRequest).all()
        assert len(requests) == 0


# ============ BuildoutRequests Tests ============


class TestBuildoutRequestsCreate:
    """Tests for BuildoutRequests.create."""

    def test_creates_buildout_request(self, db_session):
        """Test creates buildout request."""
        payload = BuildoutRequestCreate(
            requested_by="user@example.com",
            notes="Test request",
        )
        result = qualification_service.buildout_requests.create(db_session, payload)
        assert result.id is not None
        assert result.requested_by == "user@example.com"

    def test_links_to_qualification(self, db_session):
        """Test links to qualification."""
        area = CoverageArea(name="Test", geometry_geojson=VALID_POLYGON)
        db_session.add(area)
        db_session.flush()

        qualification = ServiceQualification(
            latitude=37.75,
            longitude=-122.25,
            coverage_area_id=area.id,
        )
        db_session.add(qualification)
        db_session.commit()
        db_session.refresh(qualification)

        # Explicitly pass coverage_area_id since service uses setdefault
        # and model_dump() includes all fields including None values
        payload = BuildoutRequestCreate(
            qualification_id=qualification.id,
            coverage_area_id=area.id,
        )
        result = qualification_service.buildout_requests.create(db_session, payload)
        assert result.coverage_area_id == area.id
        assert result.qualification_id == qualification.id

    def test_updates_qualification_status(self, db_session):
        """Test updates qualification status to needs_buildout."""
        qualification = ServiceQualification(
            latitude=37.75,
            longitude=-122.25,
            status=QualificationStatus.ineligible,
        )
        db_session.add(qualification)
        db_session.commit()

        payload = BuildoutRequestCreate(qualification_id=qualification.id)
        qualification_service.buildout_requests.create(db_session, payload)
        db_session.refresh(qualification)
        assert qualification.status == QualificationStatus.needs_buildout

    def test_raises_for_invalid_qualification(self, db_session):
        """Test raises for invalid qualification_id."""
        payload = BuildoutRequestCreate(qualification_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.buildout_requests.create(db_session, payload)
        assert exc_info.value.status_code == 404


class TestBuildoutRequestsGet:
    """Tests for BuildoutRequests.get."""

    def test_gets_request(self, db_session):
        """Test gets request."""
        request = BuildoutRequest(requested_by="test")
        db_session.add(request)
        db_session.commit()

        result = qualification_service.buildout_requests.get(db_session, str(request.id))
        assert result.id == request.id

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.buildout_requests.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


class TestBuildoutRequestsList:
    """Tests for BuildoutRequests.list."""

    def test_lists_requests(self, db_session):
        """Test lists requests."""
        r1 = BuildoutRequest(requested_by="user1")
        r2 = BuildoutRequest(requested_by="user2")
        db_session.add_all([r1, r2])
        db_session.commit()

        result = qualification_service.buildout_requests.list(
            db_session,
            status=None,
            coverage_area_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(result) == 2

    def test_filters_by_status(self, db_session):
        """Test filters by status."""
        submitted = BuildoutRequest(requested_by="u1", status=BuildoutRequestStatus.submitted)
        approved = BuildoutRequest(requested_by="u2", status=BuildoutRequestStatus.approved)
        db_session.add_all([submitted, approved])
        db_session.commit()

        result = qualification_service.buildout_requests.list(
            db_session,
            status="submitted",
            coverage_area_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(result) == 1
        assert result[0].status == BuildoutRequestStatus.submitted

    def test_filters_by_coverage_area_id(self, db_session):
        """Test filters by coverage_area_id."""
        area = CoverageArea(name="Test Area", geometry_geojson=VALID_POLYGON)
        db_session.add(area)
        db_session.commit()

        r1 = BuildoutRequest(requested_by="u1", coverage_area_id=area.id)
        r2 = BuildoutRequest(requested_by="u2", coverage_area_id=None)
        db_session.add_all([r1, r2])
        db_session.commit()

        result = qualification_service.buildout_requests.list(
            db_session,
            status=None,
            coverage_area_id=str(area.id),
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(result) == 1
        assert result[0].coverage_area_id == area.id


class TestBuildoutRequestsUpdate:
    """Tests for BuildoutRequests.update."""

    def test_updates_request(self, db_session):
        """Test updates request."""
        request = BuildoutRequest(requested_by="original", notes="old")
        db_session.add(request)
        db_session.commit()

        payload = BuildoutRequestUpdate(notes="updated")
        result = qualification_service.buildout_requests.update(db_session, str(request.id), payload)
        assert result.notes == "updated"

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        payload = BuildoutRequestUpdate(notes="test")
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.buildout_requests.update(db_session, str(uuid.uuid4()), payload)
        assert exc_info.value.status_code == 404


class TestBuildoutRequestsApprove:
    """Tests for BuildoutRequests.approve."""

    def test_approves_request_and_creates_project(self, db_session):
        """Test approves request and creates project."""
        request = BuildoutRequest(requested_by="user", status=BuildoutRequestStatus.submitted)
        db_session.add(request)
        db_session.commit()

        payload = BuildoutApproveRequest(
            target_ready_date=datetime.now(UTC) + timedelta(days=30),
            notes="Approved for buildout",
        )
        result = qualification_service.buildout_requests.approve(db_session, str(request.id), payload)
        assert result.id is not None
        assert result.status == BuildoutProjectStatus.planned

        db_session.refresh(request)
        assert request.status == BuildoutRequestStatus.approved

    def test_returns_existing_project_if_already_approved(self, db_session):
        """Test returns existing project if request already approved."""
        request = BuildoutRequest(
            requested_by="user",
            status=BuildoutRequestStatus.approved,
        )
        db_session.add(request)
        db_session.commit()

        project = BuildoutProject(
            request_id=request.id,
            status=BuildoutProjectStatus.in_progress,
        )
        db_session.add(project)
        db_session.commit()

        payload = BuildoutApproveRequest()
        result = qualification_service.buildout_requests.approve(db_session, str(request.id), payload)
        assert result.id == project.id

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        payload = BuildoutApproveRequest()
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.buildout_requests.approve(db_session, str(uuid.uuid4()), payload)
        assert exc_info.value.status_code == 404


# ============ BuildoutProjects Tests ============


class TestBuildoutProjectsCreate:
    """Tests for BuildoutProjects.create."""

    def test_creates_project(self, db_session):
        """Test creates project."""
        payload = BuildoutProjectCreate(
            status=BuildoutProjectStatus.planned,
            progress_percent=0,
            notes="New project",
        )
        result = qualification_service.buildout_projects.create(db_session, payload)
        assert result.id is not None
        assert result.status == BuildoutProjectStatus.planned


class TestBuildoutProjectsGet:
    """Tests for BuildoutProjects.get."""

    def test_gets_project(self, db_session):
        """Test gets project."""
        project = BuildoutProject(status=BuildoutProjectStatus.planned)
        db_session.add(project)
        db_session.commit()

        result = qualification_service.buildout_projects.get(db_session, str(project.id))
        assert result.id == project.id

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.buildout_projects.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


class TestBuildoutProjectsList:
    """Tests for BuildoutProjects.list."""

    def test_lists_projects(self, db_session):
        """Test lists projects."""
        p1 = BuildoutProject(status=BuildoutProjectStatus.planned)
        p2 = BuildoutProject(status=BuildoutProjectStatus.in_progress)
        db_session.add_all([p1, p2])
        db_session.commit()

        result = qualification_service.buildout_projects.list(
            db_session,
            status=None,
            coverage_area_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(result) == 2

    def test_filters_by_status(self, db_session):
        """Test filters by status."""
        planned = BuildoutProject(status=BuildoutProjectStatus.planned)
        completed = BuildoutProject(status=BuildoutProjectStatus.completed)
        db_session.add_all([planned, completed])
        db_session.commit()

        result = qualification_service.buildout_projects.list(
            db_session,
            status="completed",
            coverage_area_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(result) == 1
        assert result[0].status == BuildoutProjectStatus.completed

    def test_filters_by_coverage_area_id(self, db_session):
        """Test filters by coverage_area_id."""
        area = CoverageArea(name="Test Area", geometry_geojson=VALID_POLYGON)
        db_session.add(area)
        db_session.commit()

        with_area = BuildoutProject(status=BuildoutProjectStatus.planned, coverage_area_id=area.id)
        without_area = BuildoutProject(status=BuildoutProjectStatus.planned, coverage_area_id=None)
        db_session.add_all([with_area, without_area])
        db_session.commit()

        result = qualification_service.buildout_projects.list(
            db_session,
            status=None,
            coverage_area_id=str(area.id),
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(result) == 1
        assert result[0].coverage_area_id == area.id


class TestBuildoutProjectsUpdate:
    """Tests for BuildoutProjects.update."""

    def test_updates_project(self, db_session):
        """Test updates project."""
        project = BuildoutProject(status=BuildoutProjectStatus.planned, progress_percent=0)
        db_session.add(project)
        db_session.commit()

        payload = BuildoutProjectUpdate(progress_percent=50)
        result = qualification_service.buildout_projects.update(db_session, str(project.id), payload)
        assert result.progress_percent == 50

    def test_creates_update_record_on_status_change(self, db_session):
        """Test creates BuildoutUpdate on status change."""
        project = BuildoutProject(status=BuildoutProjectStatus.planned)
        db_session.add(project)
        db_session.commit()

        payload = BuildoutProjectUpdate(status=BuildoutProjectStatus.in_progress)
        qualification_service.buildout_projects.update(db_session, str(project.id), payload)

        updates = db_session.query(BuildoutUpdate).filter_by(project_id=project.id).all()
        assert len(updates) >= 1

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        payload = BuildoutProjectUpdate(progress_percent=25)
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.buildout_projects.update(db_session, str(uuid.uuid4()), payload)
        assert exc_info.value.status_code == 404


class TestBuildoutProjectsDelete:
    """Tests for BuildoutProjects.delete."""

    def test_deletes_project(self, db_session):
        """Test hard deletes project."""
        project = BuildoutProject(status=BuildoutProjectStatus.planned)
        db_session.add(project)
        db_session.commit()
        project_id = project.id

        qualification_service.buildout_projects.delete(db_session, str(project_id))
        assert db_session.get(BuildoutProject, project_id) is None

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.buildout_projects.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============ BuildoutMilestones Tests ============


class TestBuildoutMilestonesCreate:
    """Tests for BuildoutMilestones.create."""

    def test_creates_milestone(self, db_session):
        """Test creates milestone."""
        project = BuildoutProject(status=BuildoutProjectStatus.planned)
        db_session.add(project)
        db_session.commit()

        payload = BuildoutMilestoneCreate(
            project_id=project.id,
            name="Design Complete",
            order_index=1,
        )
        result = qualification_service.buildout_milestones.create(db_session, payload)
        assert result.id is not None
        assert result.name == "Design Complete"


class TestBuildoutMilestonesGet:
    """Tests for BuildoutMilestones.get."""

    def test_gets_milestone(self, db_session):
        """Test gets milestone."""
        project = BuildoutProject(status=BuildoutProjectStatus.planned)
        db_session.add(project)
        db_session.commit()

        milestone = BuildoutMilestone(project_id=project.id, name="Test")
        db_session.add(milestone)
        db_session.commit()

        result = qualification_service.buildout_milestones.get(db_session, str(milestone.id))
        assert result.id == milestone.id

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.buildout_milestones.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


class TestBuildoutMilestonesList:
    """Tests for BuildoutMilestones.list."""

    def test_lists_milestones(self, db_session):
        """Test lists milestones."""
        project = BuildoutProject(status=BuildoutProjectStatus.planned)
        db_session.add(project)
        db_session.commit()

        m1 = BuildoutMilestone(project_id=project.id, name="M1", order_index=1)
        m2 = BuildoutMilestone(project_id=project.id, name="M2", order_index=2)
        db_session.add_all([m1, m2])
        db_session.commit()

        result = qualification_service.buildout_milestones.list(
            db_session,
            project_id=str(project.id),
            status=None,
            order_by="order_index",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(result) == 2

    def test_filters_by_status(self, db_session):
        """Test filters by status."""
        project = BuildoutProject(status=BuildoutProjectStatus.planned)
        db_session.add(project)
        db_session.commit()

        pending = BuildoutMilestone(project_id=project.id, name="Pending", status=BuildoutMilestoneStatus.pending)
        completed = BuildoutMilestone(project_id=project.id, name="Done", status=BuildoutMilestoneStatus.completed)
        db_session.add_all([pending, completed])
        db_session.commit()

        result = qualification_service.buildout_milestones.list(
            db_session,
            project_id=None,
            status="completed",
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(result) == 1
        assert result[0].status == BuildoutMilestoneStatus.completed


class TestBuildoutMilestonesUpdate:
    """Tests for BuildoutMilestones.update."""

    def test_updates_milestone(self, db_session):
        """Test updates milestone."""
        project = BuildoutProject(status=BuildoutProjectStatus.planned)
        db_session.add(project)
        db_session.commit()

        milestone = BuildoutMilestone(project_id=project.id, name="Original")
        db_session.add(milestone)
        db_session.commit()

        payload = BuildoutMilestoneUpdate(name="Updated")
        result = qualification_service.buildout_milestones.update(db_session, str(milestone.id), payload)
        assert result.name == "Updated"

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        payload = BuildoutMilestoneUpdate(name="Test")
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.buildout_milestones.update(db_session, str(uuid.uuid4()), payload)
        assert exc_info.value.status_code == 404


class TestBuildoutMilestonesDelete:
    """Tests for BuildoutMilestones.delete."""

    def test_deletes_milestone(self, db_session):
        """Test hard deletes milestone."""
        project = BuildoutProject(status=BuildoutProjectStatus.planned)
        db_session.add(project)
        db_session.commit()

        milestone = BuildoutMilestone(project_id=project.id, name="Test")
        db_session.add(milestone)
        db_session.commit()
        milestone_id = milestone.id

        qualification_service.buildout_milestones.delete(db_session, str(milestone_id))
        assert db_session.get(BuildoutMilestone, milestone_id) is None

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            qualification_service.buildout_milestones.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============ BuildoutUpdates Tests ============


class TestBuildoutUpdatesCreate:
    """Tests for BuildoutUpdates.create."""

    def test_creates_update(self, db_session):
        """Test creates update."""
        project = BuildoutProject(status=BuildoutProjectStatus.planned)
        db_session.add(project)
        db_session.commit()

        payload = BuildoutUpdateCreate(
            project_id=project.id,
            status=BuildoutProjectStatus.in_progress,
            message="Work started",
        )
        result = qualification_service.buildout_updates.create(db_session, payload)
        assert result.id is not None
        assert result.message == "Work started"


class TestBuildoutUpdatesList:
    """Tests for BuildoutUpdates.list."""

    def test_lists_updates(self, db_session):
        """Test lists updates."""
        project = BuildoutProject(status=BuildoutProjectStatus.planned)
        db_session.add(project)
        db_session.commit()

        u1 = BuildoutUpdate(project_id=project.id, status=BuildoutProjectStatus.planned, message="Created")
        u2 = BuildoutUpdate(project_id=project.id, status=BuildoutProjectStatus.in_progress, message="Started")
        db_session.add_all([u1, u2])
        db_session.commit()

        result = qualification_service.buildout_updates.list(
            db_session,
            project_id=str(project.id),
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(result) == 2

    def test_lists_without_project_filter(self, db_session):
        """Test lists all updates without project filter."""
        project = BuildoutProject(status=BuildoutProjectStatus.planned)
        db_session.add(project)
        db_session.commit()

        u1 = BuildoutUpdate(project_id=project.id, status=BuildoutProjectStatus.planned)
        db_session.add(u1)
        db_session.commit()

        result = qualification_service.buildout_updates.list(
            db_session,
            project_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert len(result) >= 1
