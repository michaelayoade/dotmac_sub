"""Tests for the customer Service Location page, geocode helpers, and admin review."""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models.gis import (
    CustomerLocationChangeRequest,
    CustomerLocationChangeRequestStatus,
)
from app.models.subscriber import Subscriber
from app.services import customer_location_requests as location_service
from app.services import geocoding
from app.web.customer import location as location_web


def _subscriber(db_session):
    subscriber = Subscriber(
        first_name="Pin",
        last_name="Mover",
        email=f"pin-{uuid.uuid4().hex}@example.com",
        address_line1="12 Allen Avenue",
        city="Ikeja",
        region="Lagos",
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)
    return subscriber


def _customer(subscriber) -> dict:
    return {"subscriber_id": str(subscriber.id), "username": subscriber.email}


# =============================================================================
# reverse_geocode service
# =============================================================================


class TestReverseGeocode:
    def test_sends_expected_params(self, db_session):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "lat": "6.6018",
            "lon": "3.3515",
            "display_name": "Allen Avenue, Ikeja, Lagos, Nigeria",
            "address": {"road": "Allen Avenue"},
        }
        mock_response.raise_for_status = MagicMock()

        with patch(
            "app.services.geocoding.httpx.get", return_value=mock_response
        ) as mock_get:
            result = geocoding.reverse_geocode(db_session, 6.6018, 3.3515)

        call_args = mock_get.call_args
        assert call_args[0][0].endswith("/reverse")
        params = call_args[1]["params"]
        assert params["lat"] == 6.6018
        assert params["lon"] == 3.3515
        assert params["addressdetails"] == 1
        assert result is not None
        assert result["display_name"].startswith("Allen Avenue")
        assert result["latitude"] == 6.6018
        assert result["address"]["road"] == "Allen Avenue"

    def test_error_payload_returns_none(self, db_session):
        mock_response = MagicMock()
        mock_response.json.return_value = {"error": "Unable to geocode"}
        mock_response.raise_for_status = MagicMock()

        with patch("app.services.geocoding.httpx.get", return_value=mock_response):
            assert geocoding.reverse_geocode(db_session, 0.0, 0.0) is None

    def test_invalid_coordinates_raise(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            geocoding.reverse_geocode(db_session, 120.0, 3.0)
        assert exc_info.value.status_code == 400

    def test_disabled_returns_none(self, db_session):
        from app.models.domain_settings import DomainSetting, SettingDomain

        db_session.add(
            DomainSetting(
                domain=SettingDomain.geocoding,
                key="enabled",
                value_text="false",
                is_active=True,
            )
        )
        db_session.commit()
        assert geocoding.reverse_geocode(db_session, 6.6, 3.35) is None


# =============================================================================
# Portal routes
# =============================================================================


class TestPortalLocationRoutes:
    def test_page_redirects_when_unauthenticated(self, db_session):
        with patch(
            "app.web.customer.location.get_current_customer_from_request",
            return_value=None,
        ):
            response = location_web.customer_location_page(MagicMock(), db_session)
        assert response.status_code == 303
        assert "/portal/auth/login" in response.headers["location"]

    def test_page_renders_for_customer(self, db_session):
        subscriber = _subscriber(db_session)
        template_response = MagicMock(name="template_response")
        with (
            patch(
                "app.web.customer.location.get_current_customer_from_request",
                return_value=_customer(subscriber),
            ),
            patch(
                "app.web.customer.location.templates.TemplateResponse",
                return_value=template_response,
            ) as render,
        ):
            response = location_web.customer_location_page(MagicMock(), db_session)
        assert response is template_response
        context = render.call_args[0][1]
        assert context["active_page"] == "location"
        assert context["can_submit_request"] is True

    def test_submit_creates_pending_request(self, db_session):
        subscriber = _subscriber(db_session)
        request = MagicMock()
        request.client.host = "203.0.113.9"
        with patch(
            "app.web.customer.location.get_current_customer_from_request",
            return_value=_customer(subscriber),
        ):
            response = location_web.customer_location_submit(
                request,
                latitude=6.601234,
                longitude=3.351234,
                customer_note="Pin is one street off",
                db=db_session,
            )
        assert response.status_code == 303
        stored = (
            db_session.query(CustomerLocationChangeRequest)
            .filter(CustomerLocationChangeRequest.subscriber_id == subscriber.id)
            .one()
        )
        assert stored.status == CustomerLocationChangeRequestStatus.pending
        assert stored.requested_latitude == pytest.approx(6.601234)
        assert stored.submitted_from_ip == "203.0.113.9"

    def test_second_pending_submit_renders_error(self, db_session):
        subscriber = _subscriber(db_session)
        request = MagicMock()
        request.client.host = None
        template_response = MagicMock(name="template_response")
        with (
            patch(
                "app.web.customer.location.get_current_customer_from_request",
                return_value=_customer(subscriber),
            ),
            patch(
                "app.web.customer.location.templates.TemplateResponse",
                return_value=template_response,
            ) as render,
        ):
            first = location_web.customer_location_submit(
                request, latitude=6.6, longitude=3.35, customer_note="", db=db_session
            )
            assert first.status_code == 303
            second = location_web.customer_location_submit(
                request, latitude=6.7, longitude=3.36, customer_note="", db=db_session
            )
        assert second is template_response
        assert render.call_args[1]["status_code"] == 400
        context = render.call_args[0][1]
        assert "pending" in context["form_error"].lower()

    def test_cancel_pending_request(self, db_session):
        subscriber = _subscriber(db_session)
        created = location_service.submit_request(
            db_session,
            subscriber_id=str(subscriber.id),
            latitude=6.6,
            longitude=3.35,
            customer_note=None,
            actor_id=str(subscriber.id),
            actor_name="Pin Mover",
        )
        with patch(
            "app.web.customer.location.get_current_customer_from_request",
            return_value=_customer(subscriber),
        ):
            response = location_web.customer_location_cancel(
                MagicMock(), str(created.id), db_session
            )
        assert response.status_code == 303
        db_session.refresh(created)
        assert created.status == CustomerLocationChangeRequestStatus.cancelled

    def test_geocode_search_requires_auth(self, db_session):
        with patch(
            "app.web.customer.location.get_current_customer_from_request",
            return_value=None,
        ):
            response = location_web.customer_location_geocode_search(
                MagicMock(), q="Allen Avenue", db=db_session
            )
        assert response.status_code == 401

    def test_geocode_search_returns_suggestions(self, db_session):
        subscriber = _subscriber(db_session)
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {
                "lat": "6.6018",
                "lon": "3.3515",
                "display_name": "Allen Avenue, Ikeja",
                "class": "highway",
                "type": "secondary",
                "importance": 0.5,
            }
        ]
        mock_response.raise_for_status = MagicMock()
        with (
            patch(
                "app.web.customer.location.get_current_customer_from_request",
                return_value=_customer(subscriber),
            ),
            patch("app.services.geocoding.httpx.get", return_value=mock_response),
        ):
            response = location_web.customer_location_geocode_search(
                MagicMock(), q="Allen Avenue", db=db_session
            )
        assert response.status_code == 200
        import json

        body = json.loads(response.body)
        assert body == [
            {
                "display_name": "Allen Avenue, Ikeja",
                "latitude": 6.6018,
                "longitude": 3.3515,
            }
        ]

    def test_reverse_geocode_endpoint(self, db_session):
        subscriber = _subscriber(db_session)
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "lat": "6.6018",
            "lon": "3.3515",
            "display_name": "Allen Avenue, Ikeja",
            "address": {},
        }
        mock_response.raise_for_status = MagicMock()
        with (
            patch(
                "app.web.customer.location.get_current_customer_from_request",
                return_value=_customer(subscriber),
            ),
            patch("app.services.geocoding.httpx.get", return_value=mock_response),
        ):
            response = location_web.customer_location_reverse_geocode(
                MagicMock(), lat=6.6018, lon=3.3515, db=db_session
            )
        assert response.status_code == 200
        import json

        body = json.loads(response.body)
        assert body["display_name"] == "Allen Avenue, Ikeja"


# =============================================================================
# Admin review
# =============================================================================


class TestAdminReview:
    def _pending(self, db_session, subscriber):
        return location_service.submit_request(
            db_session,
            subscriber_id=str(subscriber.id),
            latitude=6.61,
            longitude=3.36,
            customer_note="move pin",
            actor_id=str(subscriber.id),
            actor_name="Pin Mover",
        )

    def test_approve_applies_coordinates(self, db_session):
        subscriber = _subscriber(db_session)
        pending = self._pending(db_session, subscriber)
        approved = location_service.approve_request(
            db_session,
            request_id=str(pending.id),
            actor_id="admin-1",
            actor_name="Admin",
            review_note="confirmed on site",
        )
        assert approved.status == CustomerLocationChangeRequestStatus.approved
        assert approved.applied_at is not None
        assert approved.address is not None
        assert approved.address.latitude == pytest.approx(6.61)
        assert approved.address.longitude == pytest.approx(3.36)

    def test_reject_keeps_address_untouched(self, db_session):
        subscriber = _subscriber(db_session)
        pending = self._pending(db_session, subscriber)
        rejected = location_service.reject_request(
            db_session,
            request_id=str(pending.id),
            actor_id="admin-1",
            actor_name="Admin",
            review_note="not plausible",
        )
        assert rejected.status == CustomerLocationChangeRequestStatus.rejected
        assert rejected.applied_at is None

    def test_admin_review_context_counts(self, db_session):
        subscriber = _subscriber(db_session)
        self._pending(db_session, subscriber)
        context = location_service.get_admin_review_context(
            db_session, status=CustomerLocationChangeRequestStatus.pending
        )
        assert context["pending_location_change_count"] == 1
        assert len(context["location_change_requests"]) == 1
