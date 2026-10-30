"""Database-free security and wire-contract tests for acquisition matching."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest import TestCase
from uuid import uuid4

from pydantic import ValidationError

from app.schemas.fiber_inquiry import (
    FiberAttribution,
    FiberInquiryInterest,
    FiberInquiryRequest,
    FiberLocation,
)
from app.services.website_handoff_values import (
    WebsiteHandoffFact,
    WhatsAppHandoffFact,
    handoff_reference,
    match_handoff,
)


class WebsiteHandoffPolicyTests(TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 27, 10, tzinfo=UTC)
        self.website = WebsiteHandoffFact(
            observation_id=uuid4(),
            integration_inbox_id=uuid4(),
            provider="fiber_website",
            site="fiber.dotmac.ng",
            form_version="fiber-whatsapp-v1",
            delivery_id="fiber-random-delivery",
            phone="08031234567",
            whatsapp_account_id="123456",
            submitted_at=self.now,
            attribution=FiberAttribution(
                journey_id=uuid4(),
                utm_source="facebook",
                utm_campaign="abuja_fibre",
                campaign_id="123",
                ad_id="456",
                landing_path="/coverage/",
                captured_at=self.now,
            ),
        )
        self.whatsapp = WhatsAppHandoffFact(
            provider="meta_cloud_api",
            account_id="123456",
            phone="2348031234567",
            body="New internet connection. Reference: "
            + handoff_reference(self.website.delivery_id),
            observed_at=self.now + timedelta(minutes=1),
        )

    def test_matching_reference_retains_original_attribution(self) -> None:
        result = match_handoff(
            website=self.website, whatsapp=self.whatsapp, country_code="234"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.observation_id, self.website.observation_id)
        self.assertEqual(result.attribution.campaign_id, "123")
        self.assertEqual(result.attribution.ad_id, "456")

    def test_phone_and_business_account_must_both_match(self) -> None:
        for changed in (
            replace(self.whatsapp, phone="2348099999999"),
            replace(self.whatsapp, account_id="999999"),
            replace(self.whatsapp, provider="unverified"),
        ):
            with self.subTest(changed=changed.account_id):
                self.assertIsNone(
                    match_handoff(
                        website=self.website, whatsapp=changed, country_code="234"
                    )
                )

    def test_text_labels_and_two_references_are_not_attribution(self) -> None:
        for body in (
            "I came from the Abuja Facebook ad",
            "FWA-unknown",
            self.whatsapp.body + " " + handoff_reference("another-delivery"),
        ):
            self.assertIsNone(
                match_handoff(
                    website=self.website,
                    whatsapp=replace(self.whatsapp, body=body),
                    country_code="234",
                )
            )

    def test_expired_future_and_naive_times_are_rejected(self) -> None:
        for when in (
            self.now - timedelta(seconds=1),
            self.now + timedelta(days=31),
            self.now.replace(tzinfo=None),
        ):
            self.assertIsNone(
                match_handoff(
                    website=self.website,
                    whatsapp=replace(self.whatsapp, observed_at=when),
                    country_code="234",
                )
            )

    def test_unsigned_site_and_legacy_lead_form_are_rejected(self) -> None:
        for changed in (
            replace(self.website, site="another.example"),
            replace(self.website, provider="unknown"),
            replace(self.website, form_version="fiber-coverage-v1"),
        ):
            self.assertIsNone(
                match_handoff(
                    website=changed, whatsapp=self.whatsapp, country_code="234"
                )
            )

    def test_new_wire_version_is_explicit_and_requires_account(self) -> None:
        command = FiberInquiryRequest(
            form_version="fiber-whatsapp-v1",
            full_name="Test Prospect",
            phone=self.website.phone,
            interest=FiberInquiryInterest.new_connection,
            submitted_at=self.now,
            attribution=self.website.attribution,
            location=FiberLocation(address="12 Example Street, Abuja"),
            whatsapp_account_id="123456",
        )
        self.assertTrue(command.is_whatsapp_handoff)
        self.assertFalse(command.is_coverage_request)
        with self.assertRaises(ValidationError):
            FiberInquiryRequest.model_validate_json(
                command.model_dump_json(exclude={"whatsapp_account_id"})
            )

    def test_snapshot_does_not_contain_phone_or_message(self) -> None:
        result = match_handoff(
            website=self.website, whatsapp=self.whatsapp, country_code="234"
        )
        assert result is not None
        wire = result.model_dump_json()
        self.assertNotIn(self.website.phone, wire)
        self.assertNotIn("New internet connection", wire)
