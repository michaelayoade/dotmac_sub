"""Pure, typed policy values for the website/WhatsApp attribution resolver."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from app.schemas.fiber_inquiry import FiberAttribution
from app.schemas.website_handoff import WebsiteHandoffAttribution
from app.services.customer_identity_normalization import normalize_phone_identifier

_REFERENCE = re.compile(r"(?<![A-Za-z0-9-])FWA-[A-Fa-f0-9]{24}(?![A-Za-z0-9-])")


@dataclass(frozen=True, slots=True)
class WebsiteHandoffFact:
    observation_id: UUID
    integration_inbox_id: UUID
    provider: str
    site: str
    form_version: str
    delivery_id: str
    phone: str
    whatsapp_account_id: str
    submitted_at: datetime
    attribution: FiberAttribution


@dataclass(frozen=True, slots=True)
class WhatsAppHandoffFact:
    provider: str
    account_id: str
    phone: str
    body: str
    observed_at: datetime


def handoff_reference(delivery_id: str) -> str:
    return (
        "FWA-" + hashlib.sha256(delivery_id.strip().encode()).hexdigest()[:24].upper()
    )


def reference_from_body(body: str) -> str | None:
    references = {item.upper() for item in _REFERENCE.findall(body)}
    return next(iter(references)) if len(references) == 1 else None


def match_handoff(
    *, website: WebsiteHandoffFact, whatsapp: WhatsAppHandoffFact, country_code: str
) -> WebsiteHandoffAttribution | None:
    reference = reference_from_body(whatsapp.body)
    if (
        website.provider != "fiber_website"
        or website.site != "fiber.dotmac.ng"
        or website.form_version != "fiber-whatsapp-v1"
        or whatsapp.provider != "meta_cloud_api"
        or not website.delivery_id.strip()
        or re.fullmatch(r"[0-9]{1,32}", whatsapp.account_id) is None
        or not whatsapp.account_id
        or whatsapp.account_id != website.whatsapp_account_id
        or reference != handoff_reference(website.delivery_id)
    ):
        return None
    if (
        website.submitted_at.tzinfo is None
        or whatsapp.observed_at.tzinfo is None
        or not website.submitted_at
        <= whatsapp.observed_at
        <= website.submitted_at + timedelta(days=30)
    ):
        return None
    sender = normalize_phone_identifier(
        whatsapp.phone, default_country_code=country_code
    )
    supplied = normalize_phone_identifier(
        website.phone, default_country_code=country_code
    )
    if not sender or sender != supplied:
        return None
    return WebsiteHandoffAttribution(
        observation_id=website.observation_id,
        integration_inbox_id=website.integration_inbox_id,
        reference=reference,
        submitted_at=website.submitted_at,
        attribution=website.attribution,
    )
