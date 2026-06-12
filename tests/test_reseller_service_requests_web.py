"""Tests for the reseller web portal service-requests page."""

import uuid
from unittest.mock import MagicMock, patch

from app.models.subscriber import Reseller
from app.services import web_reseller_routes


def _reseller(db_session):
    reseller = Reseller(
        name="Web SR Reseller",
        code=f"WSR{uuid.uuid4().hex[:8].upper()}",
    )
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)
    return reseller


def _context(reseller) -> dict:
    return {"reseller": reseller}


class TestServiceRequestsPage:
    def test_redirects_without_session(self, db_session):
        with patch.object(
            web_reseller_routes, "_require_reseller_context", return_value=None
        ):
            response = web_reseller_routes.reseller_service_requests_page(
                MagicMock(), db_session
            )
        assert response.status_code == 303
        assert "/reseller/auth/login" in response.headers["location"]

    def test_renders_with_requests(self, db_session):
        reseller = _reseller(db_session)
        request = MagicMock()
        request.query_params = {}
        template_response = MagicMock(name="template_response")
        with (
            patch.object(
                web_reseller_routes,
                "_require_reseller_context",
                return_value=_context(reseller),
            ),
            patch.object(
                web_reseller_routes.templates,
                "TemplateResponse",
                return_value=template_response,
            ) as render,
        ):
            response = web_reseller_routes.reseller_service_requests_page(
                request, db_session
            )
        assert response is template_response
        context = render.call_args[0][1]
        assert context["active_page"] == "service-requests"
        assert context["service_requests"] == []


class TestServiceRequestCreate:
    def test_creates_with_pin(self, db_session):
        reseller = _reseller(db_session)
        with patch.object(
            web_reseller_routes,
            "_require_reseller_context",
            return_value=_context(reseller),
        ):
            response = web_reseller_routes.reseller_service_request_create(
                MagicMock(),
                db_session,
                contact_name="Lead Person",
                contact_phone="08012345678",
                contact_email="lead@example.com",
                address="3 Awolowo Road, Ikoyi",
                latitude="6.448100",
                longitude="3.421900",
                notes="Wants 100Mbps",
            )
        assert response.status_code == 303
        assert "submitted=1" in response.headers["location"]

        from app.services import reseller_service_requests

        items = reseller_service_requests.list_for_reseller(
            db_session, str(reseller.id)
        )
        assert len(items) == 1
        assert items[0]["latitude"] == 6.4481
        assert items[0]["contact_name"] == "Lead Person"

    def test_blank_coordinates_become_none(self, db_session):
        reseller = _reseller(db_session)
        with patch.object(
            web_reseller_routes,
            "_require_reseller_context",
            return_value=_context(reseller),
        ):
            response = web_reseller_routes.reseller_service_request_create(
                MagicMock(),
                db_session,
                contact_name="No Pin",
                contact_phone="08087654321",
                contact_email="",
                address="",
                latitude="",
                longitude="",
                notes="",
            )
        assert response.status_code == 303

        from app.services import reseller_service_requests

        items = reseller_service_requests.list_for_reseller(
            db_session, str(reseller.id)
        )
        assert items[0]["latitude"] is None
        assert items[0]["longitude"] is None

    def test_missing_contact_redirects_with_error(self, db_session):
        reseller = _reseller(db_session)
        with patch.object(
            web_reseller_routes,
            "_require_reseller_context",
            return_value=_context(reseller),
        ):
            response = web_reseller_routes.reseller_service_request_create(
                MagicMock(),
                db_session,
                contact_name="",
                contact_phone="",
                contact_email="",
                address="",
                latitude="",
                longitude="",
                notes="",
            )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    def test_unpaired_coordinate_dropped(self, db_session):
        reseller = _reseller(db_session)
        with patch.object(
            web_reseller_routes,
            "_require_reseller_context",
            return_value=_context(reseller),
        ):
            web_reseller_routes.reseller_service_request_create(
                MagicMock(),
                db_session,
                contact_name="Half Pin",
                contact_phone="08011112222",
                contact_email="",
                address="",
                latitude="6.5",
                longitude="not-a-number",
                notes="",
            )
        from app.services import reseller_service_requests

        items = reseller_service_requests.list_for_reseller(
            db_session, str(reseller.id)
        )
        assert items[0]["latitude"] is None
        assert items[0]["longitude"] is None
