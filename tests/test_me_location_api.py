"""Tests for the self-scoped /me/location and /me/geocode endpoints."""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api import me as me_api
from app.models.gis import CustomerLocationChangeRequestStatus
from app.models.subscriber import Subscriber
from app.schemas.gis import MyLocationRequestCreate


def _subscriber(db_session):
    subscriber = Subscriber(
        first_name="Pin",
        last_name="Checker",
        email=f"pin-{uuid.uuid4().hex}@example.com",
        address_line1="5 Opebi Road",
        city="Ikeja",
        region="Lagos",
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)
    return subscriber


def _principal(subscriber) -> dict:
    return {
        "principal_type": "subscriber",
        "subscriber_id": str(subscriber.id),
    }


def _request():
    request = MagicMock()
    request.client.host = "203.0.113.7"
    return request


class TestMyLocation:
    def test_rejects_non_subscriber_principals(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            me_api.my_location(db_session, {"principal_type": "system_user"})
        assert exc_info.value.status_code == 403

    def test_returns_location_state(self, db_session):
        subscriber = _subscriber(db_session)
        result = me_api.my_location(db_session, _principal(subscriber))
        assert result.has_address_anchor is True
        assert result.can_submit_request is True
        assert result.pending_request is None
        assert result.history == []

    def test_submit_then_state_reflects_pending(self, db_session):
        subscriber = _subscriber(db_session)
        principal = _principal(subscriber)
        created = me_api.my_location_request_create(
            MyLocationRequestCreate(
                latitude=6.601234, longitude=3.351234, note="wrong street"
            ),
            _request(),
            db_session,
            principal,
        )
        assert created.status == CustomerLocationChangeRequestStatus.pending
        assert created.submitted_from_ip == "203.0.113.7"

        state = me_api.my_location(db_session, principal)
        assert state.can_submit_request is False
        assert state.pending_request is not None
        assert state.pending_request.requested_latitude == pytest.approx(6.601234)
        assert state.pending_request.customer_note == "wrong street"

    def test_cancel_own_request(self, db_session):
        subscriber = _subscriber(db_session)
        principal = _principal(subscriber)
        created = me_api.my_location_request_create(
            MyLocationRequestCreate(latitude=6.6, longitude=3.35, note=None),
            _request(),
            db_session,
            principal,
        )
        canceled = me_api.my_location_request_cancel(
            str(created.id), db_session, principal
        )
        assert canceled.status == CustomerLocationChangeRequestStatus.cancelled

    def test_cannot_cancel_someone_elses_request(self, db_session):
        owner = _subscriber(db_session)
        attacker = _subscriber(db_session)
        created = me_api.my_location_request_create(
            MyLocationRequestCreate(latitude=6.6, longitude=3.35, note=None),
            _request(),
            db_session,
            _principal(owner),
        )
        with pytest.raises(HTTPException) as exc_info:
            me_api.my_location_request_cancel(
                str(created.id), db_session, _principal(attacker)
            )
        assert exc_info.value.status_code == 404

    def test_coordinate_validation(self):
        with pytest.raises(ValueError):
            MyLocationRequestCreate(latitude=120.0, longitude=3.35)


class TestMyReverseGeocode:
    def test_returns_display_name(self, db_session):
        subscriber = _subscriber(db_session)
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "lat": "6.6018",
            "lon": "3.3515",
            "display_name": "Opebi Road, Ikeja, Lagos",
            "address": {},
        }
        mock_response.raise_for_status = MagicMock()
        with patch("app.services.geocoding.httpx.get", return_value=mock_response):
            result = me_api.my_reverse_geocode(
                lat=6.6018, lon=3.3515, db=db_session, principal=_principal(subscriber)
            )
        assert result["display_name"] == "Opebi Road, Ikeja, Lagos"

    def test_unknown_point_returns_none_display_name(self, db_session):
        subscriber = _subscriber(db_session)
        mock_response = MagicMock()
        mock_response.json.return_value = {"error": "Unable to geocode"}
        mock_response.raise_for_status = MagicMock()
        with patch("app.services.geocoding.httpx.get", return_value=mock_response):
            result = me_api.my_reverse_geocode(
                lat=0.0, lon=0.0, db=db_session, principal=_principal(subscriber)
            )
        assert result["display_name"] is None
