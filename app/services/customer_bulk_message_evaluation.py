"""Typed preview and flush-only materialization participant for bulk sends.

The receipt owner controls the transaction. Existing customer scope/rendering
helpers remain implementation collaborators, not a second public write path.
"""

import hmac

from sqlalchemy.orm import Session

from app.services.customer_bulk_message_contracts import (
    BulkMessageCounts,
    BulkMessageEvaluation,
    BulkMessageSpec,
)


def evaluate_bulk_message(
    db: Session, *, spec: BulkMessageSpec
) -> BulkMessageEvaluation:
    """Typed boundary for preview and the flush-only materialization participant."""
    from app.services.domain_errors import DomainError
    from app.services.owner_commands import owner_command_active

    if not spec.preview_only and not owner_command_active(
        db, owner="communications.customer_bulk_messages"
    ):
        raise DomainError(
            code="communications.customer_bulk_messages.command_required",
            message="Bulk materialization requires its command owner.",
        )
    from app.services.web_customer_actions import _evaluate_bulk_message_domain

    return _evaluate_bulk_message_domain(db=db, spec=spec)


def preview_bulk_message(db: Session, *, spec: BulkMessageSpec) -> BulkMessageCounts:
    result = evaluate_bulk_message(
        db=db, spec=spec.model_copy(update={"preview_only": True})
    )
    if not spec.confirmed or not spec.expected_impact_token:
        from app.services.domain_errors import DomainError

        raise DomainError(
            code="communications.customer_bulk_messages.invalid_command",
            message="Preview the bulk message impact before confirming.",
            retryable=False,
        )
    if not hmac.compare_digest(spec.expected_impact_token, result.impact_token):
        from app.services.domain_errors import DomainError

        raise DomainError(
            code="communications.customer_bulk_messages.impact_changed",
            message="The message impact changed. Review a new preview.",
            retryable=False,
        )
    from app.services.domain_errors import DomainError

    selection = spec.selection
    if (
        selection is None
        or selection.expected_count is None
        or not selection.expected_scope_token
    ):
        raise DomainError(
            code="communications.customer_bulk_messages.invalid_command",
            message="Preview the customer scope before confirming.",
            retryable=False,
        )
    if selection.expected_count != result.matched_count or not hmac.compare_digest(
        selection.expected_scope_token, result.scope_token
    ):
        raise DomainError(
            code="communications.customer_bulk_messages.impact_changed",
            message="The customer scope changed. Review a new preview.",
            retryable=False,
        )
    return BulkMessageCounts.model_validate(result.model_dump())


def materialize_bulk_message(
    db: Session, *, spec: BulkMessageSpec
) -> BulkMessageCounts:
    result = evaluate_bulk_message(
        db=db, spec=spec.model_copy(update={"preview_only": False})
    )
    return BulkMessageCounts.model_validate(result.model_dump())
