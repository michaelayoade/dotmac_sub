"""Published payment content adapter; Template Studio owns rendering and versions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from dotmac_template_studio import service as studio
from sqlalchemy.orm import Session

from app.services.domain_errors import DomainError
from app.services.operator_tenant import operator_tenant_id
from app.services.payment_template_adoption import require_rls_runtime_role


class PaymentEmailKind(StrEnum):
    receipt = "receipt"
    invoice_paid = "invoice_paid"


@dataclass(frozen=True)
class PublishedPaymentEmail:
    template_id: UUID
    version: int
    subject: str
    body: str


_HTML_MARKUP = re.compile(r"<\s*(?:[a-zA-Z][^>]*|![^>]*|/\s*[a-zA-Z][^>]*)>")


def supports_plain_text_composition(body: str) -> bool:
    """Refuse markup rather than concatenating independent HTML documents.

    The delivery adapter detects HTML from tags. This conservative boundary
    accepts only text; refusal leaves the original published body untouched.
    """
    return bool(body.strip()) and _HTML_MARKUP.search(body) is None


def render_payment_email(
    db: Session,
    *,
    kind: PaymentEmailKind,
    expected_template_id: UUID,
    values: dict[str, str],
) -> PublishedPaymentEmail | None:
    """Render active content; an inactive publication suppresses only email."""
    require_rls_runtime_role(db)
    tenant_id = operator_tenant_id()
    slug = "payment-received" if kind is PaymentEmailKind.receipt else "invoice-paid"
    template = studio.get_by_slug(db, tenant_id, slug, "email")
    # Capture the publication identity under the same lock as render_published.
    # Sub authoring takes this lock before changing the publication pointer.
    db.refresh(template, with_for_update=True)
    if template.id != expected_template_id or template.published_version is None:
        raise DomainError(
            code="payment_email_content.identity_changed",
            message="Reviewed published payment email identity changed",
            retryable=False,
        )
    if not template.is_active:
        return None
    subject, body = studio.render_published(
        db, tenant_id, slug, "email", values, strict=True
    )
    if not subject or not body.strip():
        raise DomainError(
            code="payment_email_content.empty_content",
            message="Published payment email is empty",
            retryable=False,
        )
    if kind is PaymentEmailKind.receipt and (
        not values.get("receipt_number")
        or not values.get("receipt_url")
        or values["receipt_number"] not in f"{subject}\n{body}"
        or values["receipt_url"] not in body
    ):
        raise DomainError(
            code="payment_email_content.missing_receipt",
            message="Published payment email lacks its receipt reference or URL",
            retryable=False,
        )
    return PublishedPaymentEmail(template.id, template.published_version, subject, body)
