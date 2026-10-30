"""Pure acquisition value policy owned by sales.lead_lifecycle."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from app.schemas.website_handoff import WebsiteHandoffAttribution


@dataclass(frozen=True, slots=True)
class AcquisitionEvidence:
    integration_inbox_id: UUID | None = None
    journey_id: UUID | None = None
    customer_reference: str | None = None
    external_campaign_id: str | None = None
    external_ad_set_id: str | None = None
    external_ad_id: str | None = None
    external_form_id: str | None = None
    external_click_id: str | None = None
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_content: str | None = None
    utm_term: str | None = None
    landing_path: str | None = None
    captured_at: datetime | None = None
    submitted_at: datetime | None = None

    @property
    def is_whatsapp_handoff(self) -> bool:
        return (
            self.external_form_id == "fiber-whatsapp-v1"
            and self.integration_inbox_id is not None
            and self.journey_id is not None
            and re.fullmatch(r"FWA-[A-F0-9]{24}", self.customer_reference or "")
            is not None
        )

    @property
    def is_fiber_origin(self) -> bool:
        return self.journey_id is not None and (
            self.external_form_id == "fiber-coverage-v1" or self.is_whatsapp_handoff
        )


def compose_inbox_acquisition(
    *, original: AcquisitionEvidence, website: WebsiteHandoffAttribution
) -> AcquisitionEvidence:
    """Preserve the native identifier family; retain website facts via its receipt.

    Never combine identifiers from different campaign observations. An existing
    receipt/form/journey is already an origin and cannot be repointed here.
    """
    if (
        original.integration_inbox_id is not None
        or original.external_form_id is not None
        or original.journey_id is not None
        or original.customer_reference is not None
    ):
        return original
    native_identifiers = any(
        (
            original.external_campaign_id,
            original.external_ad_set_id,
            original.external_ad_id,
            original.external_click_id,
        )
    )
    web = website.attribution
    return replace(
        original,
        integration_inbox_id=website.integration_inbox_id,
        journey_id=web.journey_id,
        customer_reference=website.reference,
        external_campaign_id=original.external_campaign_id
        if native_identifiers
        else web.campaign_id,
        external_ad_set_id=original.external_ad_set_id
        if native_identifiers
        else web.ad_set_id,
        external_ad_id=original.external_ad_id if native_identifiers else web.ad_id,
        external_click_id=original.external_click_id
        if native_identifiers
        else web.click_id,
        external_form_id=website.form_version,
        utm_source=original.utm_source or web.utm_source,
        utm_medium=original.utm_medium or web.utm_medium,
        utm_campaign=original.utm_campaign or web.utm_campaign,
        utm_content=original.utm_content or web.utm_content,
        utm_term=original.utm_term or web.utm_term,
        landing_path=original.landing_path or web.landing_path,
        captured_at=original.captured_at or web.captured_at,
        submitted_at=original.submitted_at or website.submitted_at,
    )
