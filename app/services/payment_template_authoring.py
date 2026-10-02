"""Sub transaction adapter for authoring the two Template Studio payment emails.

Template Studio owns content validation, revisions, and publication. Sub supplies
the authenticated operator command boundary and identifier-only event evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from dotmac_kernel.exceptions import BadRequestError, ConflictError, NotFoundError
from dotmac_template_studio import service as studio
from sqlalchemy.orm import Session

from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.notification_template_renderer import (
    validate_template_activation_text,
)
from app.services.operator_tenant import operator_tenant_id
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.payment_template_adoption import (
    PAYMENT_TEMPLATES,
    require_rls_runtime_role,
)

PaymentEmailCode = Literal["payment_received", "invoice_paid"]

_SLUGS = {item.code: item.slug for item in PAYMENT_TEMPLATES}
_COMMAND = OwnerCommandDefinition(
    owner="communications.payment_template_authoring",
    concern="payment email Template Studio publication",
    name="publish_payment_email_template",
)


def _error(suffix: str, message: str) -> DomainError:
    return DomainError(
        code=f"payment_template_authoring.{suffix}",
        message=message,
        retryable=False,
    )


@dataclass(frozen=True)
class PaymentEmailPublication:
    code: PaymentEmailCode
    template_id: UUID
    published_version: int
    is_active: bool


@dataclass(frozen=True)
class PaymentEmailDraft:
    code: PaymentEmailCode
    template_id: UUID
    published_version: int | None
    is_active: bool
    subject: str | None
    body: str | None


def _slug(code: PaymentEmailCode) -> str:
    try:
        return _SLUGS[code]
    except KeyError as exc:
        raise _error(
            "invalid_code", "Only the two payment email codes are supported."
        ) from exc


def payment_email_draft(db: Session, code: PaymentEmailCode) -> PaymentEmailDraft:
    """Read the published version for an authorized Sub operator screen."""
    tenant_id = operator_tenant_id()
    require_rls_runtime_role(db)
    try:
        template = studio.get_by_slug(db, tenant_id, _slug(code), "email")
        version = studio.get_published(db, tenant_id, template.id)
    except NotFoundError as exc:
        raise _error(
            "missing_template", "Payment email Studio template is missing."
        ) from exc
    return PaymentEmailDraft(
        code=code,
        template_id=template.id,
        published_version=template.published_version,
        is_active=template.is_active,
        subject=version.subject if version else None,
        body=version.body if version else None,
    )


def publish_payment_email_template(
    db: Session,
    *,
    context: CommandContext,
    code: PaymentEmailCode,
    expected_published_version: int,
    subject: str,
    body: str,
    is_active: bool | None = None,
) -> PaymentEmailPublication:
    """Atomically create and publish one revision after a locked version check."""
    slug = _slug(code)
    tenant_id = operator_tenant_id()
    if context.scope != str(tenant_id):
        raise _error(
            "invalid_tenant", "Operator tenant scope does not match the command."
        )
    if expected_published_version < 1:
        raise _error(
            "stale_version", "A published Studio version is required before editing."
        )
    if not subject.strip() or not body.strip():
        raise _error("invalid_content", "Payment email subject and body are required.")
    try:
        validate_template_activation_text(subject=subject, body=body, code=code)
    except ValueError as exc:
        raise _error("invalid_content", str(exc)) from exc

    def operation() -> PaymentEmailPublication:
        require_rls_runtime_role(db)
        try:
            template = studio.get_by_slug(db, tenant_id, slug, "email")
        except NotFoundError as exc:
            raise _error(
                "missing_template", "Payment email Studio template is missing."
            ) from exc
        # Studio has no compare-and-publish service method. Lock the returned
        # identity before comparing its publication pointer and writing a new
        # version; all Sub authoring enters this one command.
        db.refresh(template, with_for_update=True)
        if template.published_version != expected_published_version:
            raise _error(
                "stale_version", "Published version changed; reload before editing."
            )
        try:
            version = studio.create_version(
                db, tenant_id, template.id, subject=subject, body=body
            )
            studio.publish_version(db, tenant_id, template.id, version.version)
            if is_active is not None:
                studio.update_template(db, tenant_id, template.id, is_active=is_active)
        except (BadRequestError, ConflictError, NotFoundError) as exc:
            raise _error(
                "invalid_content", "Studio refused the payment email revision."
            ) from exc
        emit_event(
            db,
            EventType.payment_template_published,
            {
                "studio_template_id": str(template.id),
                "slug": slug,
                "channel": "email",
                "published_version": version.version,
            },
            actor="payment_template_authoring",
            defer_until_commit=True,
            dispatch_after_commit=False,
            record_only=True,
        )
        return PaymentEmailPublication(
            code=code,
            template_id=template.id,
            published_version=version.version,
            is_active=template.is_active,
        )

    return execute_owner_command(
        db, definition=_COMMAND, context=context, operation=operation
    )
