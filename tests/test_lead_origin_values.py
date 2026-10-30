"""Pure acquisition policy checks; these do not prove database acceptance."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest import TestCase
from uuid import uuid4

from app.schemas.fiber_inquiry import FiberAttribution
from app.schemas.website_handoff import WebsiteHandoffAttribution
from app.services.lead_origin_values import (
    AcquisitionEvidence,
    compose_inbox_acquisition,
)


def website() -> WebsiteHandoffAttribution:
    now = datetime(2026, 10, 30, tzinfo=UTC)
    return WebsiteHandoffAttribution(
        observation_id=uuid4(),
        integration_inbox_id=uuid4(),
        reference="FWA-" + "A" * 24,
        submitted_at=now,
        attribution=FiberAttribution(
            journey_id=uuid4(),
            captured_at=now,
            campaign_id="web-campaign",
            ad_set_id="web-adset",
            ad_id="web-ad",
            click_id="web-click",
            utm_source="meta",
            landing_path="/coverage/",
        ),
    )


class LeadOriginValuesTests(TestCase):
    def test_web_identifiers_fill_an_origin_without_native_ids(self) -> None:
        found = website()
        result = compose_inbox_acquisition(
            original=AcquisitionEvidence(), website=found
        )
        self.assertEqual(result.external_campaign_id, "web-campaign")
        self.assertEqual(result.integration_inbox_id, found.integration_inbox_id)
        self.assertEqual(result.customer_reference, found.reference)
        self.assertEqual(result.journey_id, found.attribution.journey_id)
        self.assertTrue(result.is_whatsapp_handoff)
        self.assertTrue(result.is_fiber_origin)

    def test_native_identifier_family_is_never_mixed_with_website_ids(self) -> None:
        for field in (
            "external_campaign_id",
            "external_ad_set_id",
            "external_ad_id",
            "external_click_id",
        ):
            with self.subTest(field=field):
                original = replace(AcquisitionEvidence(), **{field: "native-id"})
                result = compose_inbox_acquisition(original=original, website=website())
                self.assertEqual(getattr(result, field), "native-id")
                for other in (
                    "external_campaign_id",
                    "external_ad_set_id",
                    "external_ad_id",
                    "external_click_id",
                ):
                    if other != field:
                        self.assertIsNone(getattr(result, other))
                self.assertEqual(result.utm_source, "meta")
                self.assertTrue(result.is_whatsapp_handoff)

    def test_blank_website_fields_cannot_erase_native_evidence(self) -> None:
        found = website().model_copy(
            update={
                "attribution": FiberAttribution(
                    journey_id=uuid4(),
                    captured_at=datetime.now(UTC),
                    landing_path="/coverage/",
                )
            }
        )
        original = AcquisitionEvidence(
            external_ad_id="native-ad", utm_source="native-source"
        )
        result = compose_inbox_acquisition(original=original, website=found)
        self.assertEqual(result.external_ad_id, "native-ad")
        self.assertEqual(result.utm_source, "native-source")

    def test_an_already_captured_origin_is_not_repointed(self) -> None:
        for field, value in (
            ("integration_inbox_id", uuid4()),
            ("journey_id", uuid4()),
            ("external_form_id", "other-form"),
            ("customer_reference", "FBR-ABCDEF12"),
        ):
            with self.subTest(field=field):
                original = replace(AcquisitionEvidence(), **{field: value})
                self.assertIs(
                    compose_inbox_acquisition(original=original, website=website()),
                    original,
                )

    def test_marker_alone_does_not_make_a_reportable_handoff(self) -> None:
        incomplete = AcquisitionEvidence(
            external_form_id="fiber-whatsapp-v1", journey_id=uuid4()
        )
        self.assertFalse(incomplete.is_whatsapp_handoff)
        self.assertFalse(incomplete.is_fiber_origin)
        self.assertTrue(
            AcquisitionEvidence(
                external_form_id="fiber-coverage-v1", journey_id=uuid4()
            ).is_fiber_origin
        )
