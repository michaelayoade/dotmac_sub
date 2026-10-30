"""Typed browse inputs, real templates, and access/read architecture guards."""

import ast
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest import TestCase

from jinja2 import (
    ChoiceLoader,
    DictLoader,
    Environment,
    FileSystemLoader,
    StrictUndefined,
    select_autoescape,
)
from pydantic import TypeAdapter, ValidationError

from app.schemas.lead_acquisition import WebsiteEnquiryStatus
from app.schemas.website_enquiry import (
    EnquiryPageSize,
    EnquiryReceiptState,
    WebsiteEnquiryListQuery,
    WebsiteEnquiryPage,
    WebsiteEnquiryRow,
)
from app.services.lead_acquisition_values import project_enquiry_details
from tests.test_lead_acquisition_view import enquiry_fixture, origin_fixture

ROOT = Path(__file__).resolve().parents[1]


def enquiry_row_fixture(*, linked: bool = False) -> WebsiteEnquiryRow:
    request = enquiry_fixture()
    origin = origin_fixture(request)
    assert origin.receipt_id is not None
    return WebsiteEnquiryRow(
        receipt_id=origin.receipt_id,
        received_at=origin.created_at,
        receipt_state=EnquiryReceiptState.processed,
        reference=origin.reference,
        details_status=WebsiteEnquiryStatus.available,
        enquiry=project_enquiry_details(enquiry=request),
        linked_lead_id=origin.lead_id if linked else None,
        linked_lead_title="Existing Lead" if linked else None,
        linked_lead_active=True if linked else None,
    )


def enquiry_environment() -> Environment:
    return Environment(
        loader=ChoiceLoader(
            [
                DictLoader(
                    {
                        "layouts/admin.html": "<!doctype html><html><head><title>{% block title %}{% endblock %}</title></head><body>{% block content %}{% endblock %}</body></html>"
                    }
                ),
                FileSystemLoader(ROOT / "templates"),
            ]
        ),
        autoescape=select_autoescape(("html",)),
        undefined=StrictUndefined,
    )


class WebsiteEnquiryUITests(TestCase):
    def test_page_size_accepts_real_http_query_strings(self) -> None:
        self.assertEqual(
            TypeAdapter(EnquiryPageSize).validate_python("25"),
            EnquiryPageSize.twenty_five,
        )
        with self.assertRaises(ValidationError):
            TypeAdapter(EnquiryPageSize).validate_python("500")

    def test_query_defaults_include_all_history_and_bound_page_size(self) -> None:
        query = WebsiteEnquiryListQuery()
        self.assertIsNone(query.received_from)
        self.assertIsNone(query.received_to)
        self.assertEqual(query.per_page, 25)
        self.assertFalse(query.has_advanced_filters)
        self.assertTrue(WebsiteEnquiryListQuery(source="meta").has_advanced_filters)
        self.assertEqual(WebsiteEnquiryListQuery(search="  Abuja  ").search, "Abuja")
        for values in (
            {"page": 0},
            {"per_page": 500},
            {"received_from": date(2026, 10, 30), "received_to": date(2026, 10, 29)},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                WebsiteEnquiryListQuery.model_validate(values)

    def test_available_list_and_detail_do_not_require_a_conversation_link(self) -> None:
        row = enquiry_row_fixture()
        page = WebsiteEnquiryPage(WebsiteEnquiryListQuery(), (row,), 1, 1)
        environment = enquiry_environment()
        listing = environment.get_template(
            "admin/sales/website_enquiries/index.html"
        ).render(query=page.query, enquiries=page, previous_url=None, next_url=None)
        detail = environment.get_template(
            "admin/sales/website_enquiries/detail.html"
        ).render(enquiry_record=row, receipt_id=row.receipt_id)
        for html in (listing, detail):
            self.assertIn("Website Prospect", html)
            self.assertIn("+2348031234567", html)
            self.assertIn("abuja_fibre", html)
            self.assertNotIn('href="/admin/inbox', html)
        self.assertIn("No linked Lead", listing)
        self.assertIn("Website enquiry · as submitted", detail)
        self.assertIn("12 Example Close", detail)

    def test_linked_enquiries_open_the_existing_lead(self) -> None:
        row = enquiry_row_fixture(linked=True)
        html = (
            enquiry_environment()
            .get_template("admin/sales/website_enquiries/detail.html")
            .render(enquiry_record=row, receipt_id=row.receipt_id)
        )
        self.assertIn("/admin/sales/leads/" + str(row.linked_lead_id), html)
        self.assertNotIn("Create Lead", html)

    def test_unavailable_is_not_reported_as_zero_or_empty(self) -> None:
        environment = enquiry_environment()
        template = environment.get_template("admin/sales/website_enquiries/index.html")
        query = WebsiteEnquiryListQuery()
        unavailable = template.render(
            query=query, enquiries=None, previous_url=None, next_url=None
        )
        empty = template.render(
            query=query,
            enquiries=WebsiteEnquiryPage(query, (), 0, 1),
            previous_url=None,
            next_url=None,
        )
        self.assertIn("Enquiries unavailable", unavailable)
        self.assertNotIn("0 saved submissions", unavailable)
        self.assertIn("0 saved submissions", empty)
        self.assertIn("No saved website enquiries match", empty)
        detail = environment.get_template(
            "admin/sales/website_enquiries/detail.html"
        ).render(enquiry_record=None, receipt_id=enquiry_row_fixture().receipt_id)
        self.assertIn("Enquiry unavailable", detail)
        self.assertIn("Retry", detail)

    def test_invalid_details_remain_a_visible_row_and_submitted_text_is_escaped(
        self,
    ) -> None:
        row = enquiry_row_fixture()
        assert row.enquiry is not None
        row = replace(
            row,
            enquiry=replace(row.enquiry, full_name='<script>alert("fixture")</script>'),
        )
        query = WebsiteEnquiryListQuery(search='"><script>fixture</script>')
        template = enquiry_environment().get_template(
            "admin/sales/website_enquiries/index.html"
        )
        html = template.render(
            query=query,
            enquiries=WebsiteEnquiryPage(query, (row,), 1, 1),
            previous_url=None,
            next_url=None,
        )
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        invalid = replace(
            row, enquiry=None, details_status=WebsiteEnquiryStatus.invalid
        )
        html = template.render(
            query=query,
            enquiries=WebsiteEnquiryPage(query, (invalid,), 1, 1),
            previous_url=None,
            next_url=None,
        )
        self.assertIn("Details unavailable", html)
        self.assertIn("1 saved submission", html)

    def test_routes_require_both_permissions_and_reader_cannot_change_history(
        self,
    ) -> None:
        tree = ast.parse((ROOT / "app/web/admin/sales.py").read_text(encoding="utf-8"))
        for name in ("website_enquiries_list", "website_enquiry_detail"):
            route = next(
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == name
            )
            decorators = " ".join(ast.unparse(node) for node in route.decorator_list)
            self.assertIn("crm:lead:read", decorators)
            self.assertIn("support:ticket:read", decorators)
        reader = (ROOT / "app/services/website_enquiry_queries.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(reader)
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertFalse(
            calls
            & {"commit", "flush", "add", "delete", "create_party_lead", "emit_event"}
        )
        self.assertNotIn("InboxConversation", reader)
        self.assertNotIn("InboxMessage", reader)
        self.assertIn("FIBER_INQUIRY_CAPABILITY", reader)
