"""Website enquiry browse reads owned by sales.lead_lifecycle; no writers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.models.integration_platform import IntegrationInbox
from app.models.sales import Lead, LeadOriginCapture
from app.schemas.fiber_inquiry import FiberInquiryRequest
from app.schemas.lead_acquisition import WebsiteEnquiryStatus
from app.schemas.website_enquiry import (
    EnquiryLeadFilter,
    EnquiryReceiptState,
    WebsiteEnquiryDetailQuery,
    WebsiteEnquiryListQuery,
    WebsiteEnquiryPage,
    WebsiteEnquiryRow,
    WebsiteReceiptConsequence,
)
from app.services.integrations.connectors.fiber_inquiry_http import (
    FIBER_INQUIRY_CAPABILITY,
)
from app.services.lead_acquisition_values import project_enquiry_details


def _literal_search(value: str) -> str:
    return (
        "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    )


def _statement() -> Select[tuple[IntegrationInbox, LeadOriginCapture, Lead]]:
    # Receipt identity, not conversation status, determines membership in this list.
    return (
        select(IntegrationInbox, LeadOriginCapture, Lead)
        .select_from(IntegrationInbox)
        .outerjoin(
            LeadOriginCapture,
            LeadOriginCapture.integration_inbox_id == IntegrationInbox.id,
        )
        .outerjoin(Lead, Lead.id == LeadOriginCapture.lead_id)
        .where(IntegrationInbox.event_type == FIBER_INQUIRY_CAPABILITY)
    )


def list_website_enquiries(
    db: Session, *, query: WebsiteEnquiryListQuery
) -> WebsiteEnquiryPage:
    statement = _statement()
    payload = IntegrationInbox.payload_json
    if query.search:
        text = _literal_search(query.search)
        fields = (
            payload["full_name"].as_string(),
            payload["phone"].as_string(),
            payload["email"].as_string(),
            payload["location"]["address"].as_string(),
            payload["location"]["area"].as_string(),
            payload["attribution"]["utm_campaign"].as_string(),
            IntegrationInbox.consequence_json["reference"].as_string(),
            LeadOriginCapture.customer_reference,
        )
        statement = statement.where(
            or_(*(field.ilike(text, escape="\\") for field in fields))
        )
    if query.source:
        statement = statement.where(
            func.lower(payload["attribution"]["utm_source"].as_string())
            == query.source.lower()
        )
    if query.campaign:
        statement = statement.where(
            payload["attribution"]["utm_campaign"].as_string() == query.campaign
        )
    if query.received_from:
        statement = statement.where(
            IntegrationInbox.received_at
            >= datetime.combine(query.received_from, time.min, UTC)
        )
    if query.received_to:
        statement = statement.where(
            IntegrationInbox.received_at
            <= datetime.combine(query.received_to, time.max, UTC)
        )
    if query.lead_filter is EnquiryLeadFilter.linked:
        statement = statement.where(LeadOriginCapture.id.is_not(None))
    elif query.lead_filter is EnquiryLeadFilter.unlinked:
        statement = statement.where(LeadOriginCapture.id.is_(None))
    total = int(db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    page_size = int(query.per_page)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page_number = min(query.page, total_pages)
    normalized = query.model_copy(update={"page": page_number})
    records = db.execute(
        statement.order_by(
            IntegrationInbox.received_at.desc(), IntegrationInbox.id.desc()
        )
        .limit(page_size)
        .offset((page_number - 1) * page_size)
    ).all()
    return WebsiteEnquiryPage(
        query=normalized,
        items=tuple(
            _project_row(receipt=receipt, origin=origin, lead=lead)
            for receipt, origin, lead in records
        ),
        total=total,
        total_pages=total_pages,
    )


def get_website_enquiry(
    db: Session, *, query: WebsiteEnquiryDetailQuery
) -> WebsiteEnquiryRow | None:
    record = db.execute(
        _statement().where(IntegrationInbox.id == query.receipt_id)
    ).one_or_none()
    if record is None:
        return None
    return _project_row(receipt=record[0], origin=record[1], lead=record[2])


def _project_row(
    *, receipt: IntegrationInbox, origin: LeadOriginCapture | None, lead: Lead | None
) -> WebsiteEnquiryRow:
    try:
        state = EnquiryReceiptState(receipt.state)
    except ValueError:
        state = EnquiryReceiptState.unknown
    try:
        confirmed = WebsiteReceiptConsequence.model_validate(
            receipt.consequence_json or {}
        )
        reference = confirmed.reference
    except ValueError:
        reference = None
    if origin is not None and origin.customer_reference:
        reference = origin.customer_reference
    status = WebsiteEnquiryStatus.available
    details = None
    try:
        request = FiberInquiryRequest.model_validate_json(
            json.dumps(receipt.payload_json)
        )
        if origin is not None and (
            request.form_version != origin.external_form_id
            or (request.attribution.journey_id if request.attribution else None)
            != origin.journey_id
        ):
            status = WebsiteEnquiryStatus.invalid
        else:
            details = project_enquiry_details(enquiry=request)
    except (TypeError, ValueError):
        status = WebsiteEnquiryStatus.invalid
    received_at = receipt.received_at
    return WebsiteEnquiryRow(
        receipt_id=receipt.id,
        received_at=received_at.replace(tzinfo=UTC)
        if received_at.tzinfo is None
        else received_at.astimezone(UTC),
        receipt_state=state,
        reference=reference,
        details_status=status,
        enquiry=details,
        linked_lead_id=origin.lead_id if origin is not None else None,
        linked_lead_title=lead.title if lead is not None else None,
        linked_lead_active=lead.is_active if lead is not None else None,
    )
