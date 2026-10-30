"""Pure acquisition read policy and actual Jinja fragment rendering."""

import ast
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest import TestCase
from uuid import uuid4

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from app.schemas.fiber_inquiry import FiberInquiryRequest
from app.schemas.lead_acquisition import (
    AcquisitionTags,
    LeadAcquisitionOrigin,
    LeadAcquisitionView,
    WebsiteEnquiryStatus,
)
from app.services.lead_acquisition_values import project_website_enquiry

ROOT = Path(__file__).resolve().parents[1]


def enquiry_fixture() -> FiberInquiryRequest:
    return FiberInquiryRequest.model_validate_json(
        json.dumps(
            {
                "form_version": "fiber-whatsapp-v1",
                "full_name": "Website Prospect",
                "phone": "+2348031234567",
                "email": "fixture@example.com",
                "interest": "new_connection",
                "submitted_at": "2026-10-30T12:30:00Z",
                "whatsapp_account_id": "123456",
                "message": "Please check availability.",
                "attribution": {
                    "journey_id": str(uuid4()),
                    "utm_source": "meta",
                    "utm_medium": "paid_social",
                    "utm_campaign": "abuja_fibre",
                    "campaign_id": "website-campaign",
                    "ad_id": "website-ad",
                    "landing_path": "/coverage/",
                    "captured_at": "2026-10-30T12:15:00Z",
                },
                "location": {
                    "address": "12 Example Close, Wuse 2, Abuja",
                    "area": "Wuse 2",
                },
                "selected_plan": {"name": "Home Elite"},
            }
        )
    )


def origin_fixture(enquiry: FiberInquiryRequest) -> LeadAcquisitionOrigin:
    assert enquiry.attribution is not None
    return LeadAcquisitionOrigin(
        origin_id=uuid4(),
        lead_id=uuid4(),
        channel="Whatsapp",
        capture_method="inbox_classification",
        form_version="fiber-whatsapp-v1",
        captured_at=enquiry.attribution.captured_at,
        created_at=datetime(2026, 10, 30, 12, 40, tzinfo=UTC),
        submitted_at=enquiry.submitted_at,
        receipt_id=uuid4(),
        journey_id=enquiry.attribution.journey_id,
        reference="FWA-" + "A" * 24,
        tags=AcquisitionTags(source="meta", campaign="abuja_fibre", ad_id="native-ad"),
    )


def render_acquisition(view: LeadAcquisitionView) -> str:
    environment = Environment(
        loader=FileSystemLoader(ROOT / "templates"),
        autoescape=select_autoescape(("html",)),
        undefined=StrictUndefined,
    )
    return environment.get_template("admin/sales/leads/_acquisition.html").render(
        acquisition=view
    )


class LeadAcquisitionViewTests(TestCase):
    def test_original_enquiry_and_recorded_ids_remain_distinct(self) -> None:
        enquiry = enquiry_fixture()
        origin = origin_fixture(enquiry)
        view = project_website_enquiry(origin=origin, enquiry=enquiry)
        self.assertEqual(view.enquiry_status, WebsiteEnquiryStatus.available)
        self.assertIs(view.origin, origin)
        assert view.enquiry is not None
        self.assertEqual(view.enquiry.full_name, "Website Prospect")
        self.assertEqual(view.enquiry.address, "12 Example Close, Wuse 2, Abuja")
        self.assertEqual(view.origin.tags.ad_id, "native-ad")
        self.assertEqual(view.enquiry.tags.ad_id, "website-ad")
        html = render_acquisition(view)
        for fact in (
            "Website Prospect",
            "+2348031234567",
            "Wuse 2",
            "Home Elite",
            "abuja_fibre",
            origin.reference,
            "native-ad",
            "website-ad",
        ):
            self.assertIn(fact, html)
        self.assertIn("as submitted", html)

    def test_a_different_journey_or_form_is_not_shown_as_this_enquiry(self) -> None:
        enquiry = enquiry_fixture()
        origin = origin_fixture(enquiry)
        for other in (
            replace(origin, journey_id=uuid4()),
            replace(origin, form_version="fiber-coverage-v1"),
        ):
            with self.subTest(origin=other):
                view = project_website_enquiry(origin=other, enquiry=enquiry)
                self.assertEqual(view.enquiry_status, WebsiteEnquiryStatus.invalid)
                self.assertIsNone(view.enquiry)
                self.assertIs(view.origin, other)

    def test_missing_and_unavailable_states_are_distinct_and_do_not_guess(self) -> None:
        origin = origin_fixture(enquiry_fixture())
        cases = (
            (
                LeadAcquisitionView(None, WebsiteEnquiryStatus.not_recorded),
                "No acquisition source was recorded",
            ),
            (
                LeadAcquisitionView(origin, WebsiteEnquiryStatus.not_linked),
                "No website enquiry is linked",
            ),
            (
                LeadAcquisitionView(origin, WebsiteEnquiryStatus.invalid),
                "could not be verified",
            ),
            (
                LeadAcquisitionView(origin, WebsiteEnquiryStatus.unavailable),
                "could not be loaded",
            ),
        )
        for view, text in cases:
            with self.subTest(status=view.enquiry_status):
                html = render_acquisition(view)
                self.assertIn(text, html)
                self.assertNotIn("Website Prospect", html)

    def test_submitted_text_and_tracking_tags_are_html_escaped(self) -> None:
        enquiry = enquiry_fixture()
        assert enquiry.attribution is not None
        hostile = '<script>alert("fixture")</script>'
        enquiry = enquiry.model_copy(
            update={
                "full_name": hostile,
                "message": hostile,
                "attribution": enquiry.attribution.model_copy(
                    update={"utm_campaign": hostile}
                ),
            }
        )
        html = render_acquisition(
            project_website_enquiry(origin=origin_fixture(enquiry), enquiry=enquiry)
        )
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("payload_json", html)

    def test_optional_data_and_zero_coordinates_render_without_false_missing_values(
        self,
    ) -> None:
        enquiry = enquiry_fixture()
        assert enquiry.location is not None
        enquiry = enquiry.model_copy(
            update={
                "email": None,
                "selected_plan": None,
                "message": None,
                "location": enquiry.location.model_copy(
                    update={"latitude": Decimal(0), "longitude": Decimal(0)}
                ),
            }
        )
        view = project_website_enquiry(origin=origin_fixture(enquiry), enquiry=enquiry)
        html = render_acquisition(view)
        self.assertIn("Not recorded", html)
        self.assertIn(">0</dd>", html)
        self.assertNotIn("Submitted message", html)

    def test_page_uses_the_typed_owner_read_without_origin_writes_or_matching(
        self,
    ) -> None:
        source = (ROOT / "app/services/sales/lifecycle.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        query = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "get_lead_acquisition_detail"
        )
        calls = {
            node.func.attr
            for node in ast.walk(query)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertFalse(
            calls
            & {
                "commit",
                "flush",
                "add",
                "delete",
                "resolve_for_conversation",
                "resolve_for_message",
            }
        )
        web = (ROOT / "app/services/web_sales.py").read_text(encoding="utf-8")
        self.assertIn("-> LeadDetailContext:", web)
        self.assertIn("lead_lifecycle.get_lead_acquisition_detail(", web)
        template = (ROOT / "templates/admin/sales/leads/detail.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('include "admin/sales/leads/_acquisition.html"', template)
