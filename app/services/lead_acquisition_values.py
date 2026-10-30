"""Acquisition read projection policy owned by sales.lead_lifecycle."""

from datetime import UTC

from app.schemas.fiber_inquiry import FiberInquiryRequest
from app.schemas.lead_acquisition import (
    AcquisitionTags,
    LeadAcquisitionOrigin,
    LeadAcquisitionView,
    WebsiteEnquiryDetails,
    WebsiteEnquiryStatus,
)


def project_website_enquiry(
    *, origin: LeadAcquisitionOrigin, enquiry: FiberInquiryRequest
) -> LeadAcquisitionView:
    """Only project a validated enquiry belonging to this captured form/journey."""
    attribution = enquiry.attribution
    if (
        enquiry.form_version != origin.form_version
        or (attribution.journey_id if attribution else None) != origin.journey_id
    ):
        return LeadAcquisitionView(origin, WebsiteEnquiryStatus.invalid)
    return LeadAcquisitionView(
        origin=origin,
        enquiry_status=WebsiteEnquiryStatus.available,
        enquiry=project_enquiry_details(enquiry=enquiry),
    )


def project_enquiry_details(*, enquiry: FiberInquiryRequest) -> WebsiteEnquiryDetails:
    """Allowlisted submitted facts, shared by linked and unlinked read views."""
    attribution = enquiry.attribution
    location = enquiry.location
    return WebsiteEnquiryDetails(
        full_name=enquiry.full_name,
        phone=enquiry.phone,
        email=str(enquiry.email) if enquiry.email else None,
        address=location.address if location else None,
        area=location.area if location else None,
        latitude=location.latitude if location else None,
        longitude=location.longitude if location else None,
        selected_plan=enquiry.selected_plan.name if enquiry.selected_plan else None,
        message=enquiry.message,
        submitted_at=enquiry.submitted_at.astimezone(UTC),
        captured_at=attribution.captured_at.astimezone(UTC) if attribution else None,
        tags=AcquisitionTags(
            source=attribution.utm_source,
            medium=attribution.utm_medium,
            campaign=attribution.utm_campaign,
            content=attribution.utm_content,
            term=attribution.utm_term,
            campaign_id=attribution.campaign_id,
            ad_set_id=attribution.ad_set_id,
            ad_id=attribution.ad_id,
            click_id=attribution.click_id,
            landing_path=attribution.landing_path,
        )
        if attribution
        else AcquisitionTags(),
    )
