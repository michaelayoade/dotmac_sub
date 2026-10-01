"""Explicit, dormant adoption of Sub payment email content into Template Studio.

Template Studio owns the published content. This coordinator only copies a
verified legacy snapshot once; it never edits an existing Studio template.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from dotmac_kernel.exceptions import BadRequestError, NotFoundError
from dotmac_template_studio import RenderContext, register_contexts
from dotmac_template_studio import service as studio
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.notification import (
    NotificationChannel,
    NotificationTemplate,
    NotificationTemplatePurpose,
)
from app.services.domain_errors import DomainError
from app.services.events.handlers.notification import (
    EVENT_NOTIFICATION_SPECS,
    EventNotificationSpec,
)
from app.services.events.types import EventType
from app.services.notification_template_conditions import (
    NotificationTemplateConditionError,
    validate_conditions,
)
from app.services.notification_template_renderer import (
    render_template_text,
    validate_template_activation_text,
)
from app.services.operator_tenant import operator_tenant_id
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)


@dataclass(frozen=True)
class PaymentTemplateIdentity:
    event_type: EventType
    code: str
    slug: str
    context: str


PAYMENT_TEMPLATES = (
    PaymentTemplateIdentity(
        EventType.payment_received,
        "payment_received",
        "payment-received",
        "sub_payment_receipt_email",
    ),
    PaymentTemplateIdentity(
        EventType.invoice_paid,
        "invoice_paid",
        "invoice-paid",
        "sub_invoice_paid_email",
    ),
)

# These sets follow the two actual event payloads plus the handler's defaults.
# A receipt can be unallocated, so invoice fields are not in its vocabulary.
_RECEIPT_VARIABLES = (
    "subscriber_name",
    "amount",
    "portal_url",
    "receipt_number",
    "receipt_url",
)
_INVOICE_PAID_VARIABLES = (
    "subscriber_name",
    "amount",
    "invoice_number",
    "portal_url",
    "invoice_url",
)
_RECEIPT_CONTEXT = RenderContext(
    name="sub_payment_receipt_email",
    variables=_RECEIPT_VARIABLES,
    description="Sub payment receipt event email context",
)
_INVOICE_PAID_CONTEXT = RenderContext(
    name="sub_invoice_paid_email",
    variables=_INVOICE_PAID_VARIABLES,
    description="Sub invoice-paid event email context",
)
register_contexts(_RECEIPT_CONTEXT, _INVOICE_PAID_CONTEXT)
_CONTEXTS = {
    _RECEIPT_CONTEXT.name: _RECEIPT_CONTEXT,
    _INVOICE_PAID_CONTEXT.name: _INVOICE_PAID_CONTEXT,
}

REPRESENTATIVE_PAYMENT_EMAIL_CONTEXTS: Mapping[str, Sequence[Mapping[str, str]]] = {
    "payment_received": (
        {
            "subscriber_name": "Ada Example",
            "amount": "₦12,500.00",
            "portal_url": "https://example.invalid/portal",
            "receipt_number": "R-EXAMPLE-1",
            "receipt_url": "https://example.invalid/portal/receipts/1",
        },
        {
            "subscriber_name": "Chinyere & Co",
            "amount": "₦0.01",
            "portal_url": "https://example.invalid/portal",
            "receipt_number": "R-EXAMPLE-2",
            "receipt_url": "https://example.invalid/portal/receipts/2",
        },
    ),
    "invoice_paid": (
        {
            "subscriber_name": "Ada Example",
            "amount": "₦12,500.00",
            "invoice_number": "INV-EXAMPLE-1",
            "portal_url": "https://example.invalid/portal",
            "invoice_url": "https://example.invalid/portal/invoices/1",
        },
        {
            "subscriber_name": "Chinyere & Co",
            "amount": "₦0.01",
            "invoice_number": "INV-EXAMPLE-2",
            "portal_url": "https://example.invalid/portal",
            "invoice_url": "https://example.invalid/portal/invoices/2",
        },
    ),
}

_COMMAND = OwnerCommandDefinition(
    owner="communications.payment_template_adoption",
    concern="explicit payment email content adoption",
    name="adopt_payment_email_templates",
)


def _error(code: str, message: str) -> DomainError:
    return DomainError(
        code=f"payment_template_adoption.{code}",
        message=message,
        retryable=False,
    )


def _require_rls_runtime_role(db: Session) -> None:
    """Refuse parity or adoption when the current PostgreSQL role bypasses RLS."""
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        # SQLite exercises content behavior only; it cannot prove RLS isolation.
        return
    refused = _error(
        "unsafe_runtime_role",
        "Payment email adoption requires an RLS-enforced database role.",
    )
    if dialect != "postgresql":
        raise refused
    try:
        posture = db.execute(
            text(
                "SELECT rolsuper, rolbypassrls FROM pg_catalog.pg_roles "
                "WHERE rolname = current_user"
            )
        ).one_or_none()
    except SQLAlchemyError:
        raise refused from None
    if posture is None or posture.rolsuper or posture.rolbypassrls:
        raise refused


@dataclass(frozen=True)
class LegacySnapshot:
    identity: PaymentTemplateIdentity
    template_id: UUID
    legacy_code: str
    name: str
    active: bool
    purpose: NotificationTemplatePurpose
    conditions: Mapping[str, object]
    subject: str
    body: str
    provenance: str


@dataclass(frozen=True)
class AdoptionItem:
    code: str
    legacy_template_id: UUID
    studio_template_id: UUID
    published_version: int
    created: bool


@dataclass(frozen=True)
class AdoptionResult:
    tenant_id: UUID
    items: tuple[AdoptionItem, ...]


@dataclass(frozen=True)
class ReviewedPaymentEmailTemplates:
    payment_received_legacy_id: UUID
    invoice_paid_legacy_id: UUID


class StudioTemplate(Protocol):
    """The published service's metadata surface used for adoption checks."""

    id: UUID
    context: str
    name: str
    description: str | None
    is_active: bool


class ParityStatus(StrEnum):
    match = "match"
    mismatch = "mismatch"
    studio_missing = "studio_missing"
    legacy_invalid = "legacy_invalid"
    studio_invalid = "studio_invalid"


@dataclass(frozen=True)
class ParityItem:
    code: str
    legacy_template_id: UUID | None
    studio_template_id: UUID | None
    status: ParityStatus
    sample_count: int
    active: bool | None
    conditions: Mapping[str, object] | None
    purpose: NotificationTemplatePurpose | None = None


@dataclass(frozen=True)
class ParityReport:
    tenant_id: UUID
    items: tuple[ParityItem, ...]


def _legacy_snapshot(
    db: Session, identity: PaymentTemplateIdentity, *, lock: bool = False
) -> LegacySnapshot:
    statement = select(NotificationTemplate).where(
        NotificationTemplate.code.in_((identity.code, f"{identity.code}_email")),
        NotificationTemplate.channel == NotificationChannel.email,
    )
    if lock:
        statement = statement.with_for_update()
    candidates = db.scalars(statement).all()
    if len(candidates) != 1:
        raise _error(
            "ambiguous_legacy",
            f"Exactly one legacy email row is required for {identity.code}.",
        )
    row = candidates[0]
    spec: EventNotificationSpec = EVENT_NOTIFICATION_SPECS[identity.event_type]
    subject = row.subject or spec.subject
    body = row.body or spec.body
    try:
        validate_template_activation_text(
            subject=subject, body=body, code=identity.code
        )
        studio.validate_template_text(
            subject,
            body,
            context=_CONTEXTS[identity.context],
            kind_hint="adoption",
        )
        validate_conditions(row.conditions)
    except (ValueError, BadRequestError, NotificationTemplateConditionError) as exc:
        raise _error(
            "invalid_legacy", f"Invalid legacy {identity.code} email content."
        ) from exc
    if not subject.strip() or not body.strip():
        raise _error("invalid_legacy", f"Empty legacy {identity.code} email content.")
    if not isinstance(row.conditions, dict):
        raise _error("invalid_legacy", f"Invalid legacy {identity.code} conditions.")
    fingerprint = {
        "id": str(row.id),
        "code": row.code,
        "channel": row.channel.value,
        "name": row.name,
        "subject": row.subject,
        "body": row.body,
        "conditions": row.conditions,
        "is_active": row.is_active,
        "purpose": row.purpose.value,
        "effective_subject": subject,
        "effective_body": body,
    }
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return LegacySnapshot(
        identity=identity,
        template_id=row.id,
        legacy_code=row.code,
        name=row.name,
        active=bool(row.is_active),
        purpose=row.purpose,
        conditions=dict(row.conditions),
        subject=subject,
        body=body,
        provenance=f"Sub legacy NotificationTemplate {row.id} sha256:{digest}",
    )


def _studio_template(db: Session, tenant_id: UUID, slug: str) -> StudioTemplate | None:
    try:
        return studio.get_by_slug(db, tenant_id, slug, "email")
    except NotFoundError:
        return None


def _require_existing_match(
    db: Session,
    tenant_id: UUID,
    snapshot: LegacySnapshot,
    template: StudioTemplate,
) -> int:
    if (
        template.context != snapshot.identity.context
        or template.name != snapshot.name
        or template.description != snapshot.provenance
        or template.is_active != snapshot.active
    ):
        raise _error(
            "studio_conflict",
            f"Existing Studio {snapshot.identity.slug}/email differs from the adoption snapshot.",
        )
    versions = studio.list_versions(db, tenant_id, template.id)
    published = studio.get_published(db, tenant_id, template.id)
    if (
        len(versions) != 1
        or published is None
        or published.version != 1
        or published.subject != snapshot.subject
        or published.body != snapshot.body
    ):
        raise _error(
            "studio_conflict",
            f"Existing Studio {snapshot.identity.slug}/email has content or draft changes.",
        )
    return published.version


def _adopt_in_transaction(
    db: Session, reviewed: ReviewedPaymentEmailTemplates
) -> AdoptionResult:
    _require_rls_runtime_role(db)
    tenant_id = operator_tenant_id()
    snapshots = tuple(
        _legacy_snapshot(db, item, lock=True) for item in PAYMENT_TEMPLATES
    )
    if (
        snapshots[0].template_id != reviewed.payment_received_legacy_id
        or snapshots[1].template_id != reviewed.invoice_paid_legacy_id
    ):
        raise _error(
            "ambiguous_legacy",
            "Reviewed legacy payment email identities changed; refresh parity before adoption.",
        )
    parity = payment_email_parity_report(db)
    if (
        len(parity.items) != len(snapshots)
        or {item.code for item in parity.items}
        != {
            "payment_received",
            "invoice_paid",
        }
        or any(
            item.status not in {ParityStatus.studio_missing, ParityStatus.match}
            or item.legacy_template_id != snapshot.template_id
            for item, snapshot in zip(parity.items, snapshots, strict=True)
        )
    ):
        raise _error(
            "studio_conflict",
            "Payment email parity changed; review current content before adoption.",
        )
    existing = tuple(
        _studio_template(db, tenant_id, item.slug) for item in PAYMENT_TEMPLATES
    )
    # Check both before writing either. A conflict never replaces an operator edit.
    for snapshot, template in zip(snapshots, existing, strict=True):
        if template is not None:
            _require_existing_match(db, tenant_id, snapshot, template)

    results: list[AdoptionItem] = []
    for snapshot, template in zip(snapshots, existing, strict=True):
        created = template is None
        if template is None:
            template = studio.create_template(
                db,
                tenant_id,
                slug=snapshot.identity.slug,
                channel="email",
                context=snapshot.identity.context,
                name=snapshot.name,
                description=snapshot.provenance,
            )
            version = studio.create_version(
                db,
                tenant_id,
                template.id,
                subject=snapshot.subject,
                body=snapshot.body,
            )
            studio.publish_version(db, tenant_id, template.id, version.version)
            if not snapshot.active:
                studio.update_template(db, tenant_id, template.id, is_active=False)
        results.append(
            AdoptionItem(
                code=snapshot.identity.code,
                legacy_template_id=snapshot.template_id,
                studio_template_id=template.id,
                published_version=1,
                created=created,
            )
        )
    return AdoptionResult(tenant_id=tenant_id, items=tuple(results))


def adopt_payment_email_templates(
    db: Session,
    *,
    context: CommandContext,
    reviewed: ReviewedPaymentEmailTemplates,
) -> AdoptionResult:
    """Run the explicit atomic backfill through Sub's owner command boundary."""
    if context.scope != str(operator_tenant_id()):
        raise _error(
            "invalid_tenant", "Adoption context must name the operator tenant."
        )
    return execute_owner_command(
        db,
        definition=_COMMAND,
        context=context,
        operation=lambda: _adopt_in_transaction(db, reviewed),
    )


def payment_email_parity_report(
    db: Session,
    *,
    contexts: Mapping[str, Sequence[Mapping[str, str]]] | None = None,
) -> ParityReport:
    """Compare current rendered content on caller-supplied representative values.

    This is read-only. A missing row or invalid content is reported as evidence,
    not repaired. The caller must provide at least one context for each code.
    """
    _require_rls_runtime_role(db)
    if contexts is None:
        contexts = REPRESENTATIVE_PAYMENT_EMAIL_CONTEXTS
    tenant_id = operator_tenant_id()
    items: list[ParityItem] = []
    for identity in PAYMENT_TEMPLATES:
        samples = contexts.get(identity.code, ())
        if not samples:
            raise _error("invalid_contexts", f"No parity contexts for {identity.code}.")
        try:
            snapshot = _legacy_snapshot(db, identity)
        except DomainError:
            items.append(
                ParityItem(
                    identity.code,
                    None,
                    None,
                    ParityStatus.legacy_invalid,
                    len(samples),
                    None,
                    None,
                )
            )
            continue
        template = _studio_template(db, tenant_id, identity.slug)
        if template is None:
            items.append(
                ParityItem(
                    identity.code,
                    snapshot.template_id,
                    None,
                    ParityStatus.studio_missing,
                    len(samples),
                    snapshot.active,
                    snapshot.conditions,
                    snapshot.purpose,
                )
            )
            continue
        try:
            _require_existing_match(db, tenant_id, snapshot, template)
            published = studio.get_published(db, tenant_id, template.id)
            assert published is not None
            from dotmac_template_studio.rendering import render

            matches = all(
                (
                    render_template_text(snapshot.subject, values),
                    render_template_text(snapshot.body, values),
                )
                == (
                    render(published.subject or "", dict(values), strict=True),
                    render(published.body, dict(values), strict=True),
                )
                for values in samples
            )
            status = ParityStatus.match if matches else ParityStatus.mismatch
        except DomainError:
            status = ParityStatus.mismatch
        except (ValueError, KeyError):
            status = ParityStatus.studio_invalid
        items.append(
            ParityItem(
                identity.code,
                snapshot.template_id,
                template.id,
                status,
                len(samples),
                snapshot.active,
                snapshot.conditions,
                snapshot.purpose,
            )
        )
    return ParityReport(tenant_id=tenant_id, items=tuple(items))
