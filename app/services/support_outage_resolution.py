"""Support-owned immutable shared scope snapshots; Network owns scope/restoration."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.shared_outage_completion import (
    SupportResolutionSharedIncident,
    SupportResolutionSharedWorkScope,
)
from app.services.domain_errors import DomainError
from app.services.topology.outage_completion_contracts import (
    ComplaintResolutionScope,
    EmptySharedScopeReview,
    ReviewedComplaintResolutionScope,
)


def _material(
    scopes: tuple[ComplaintResolutionScope, ...],
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            str(x.complaint_link_id),
            str(x.incident_id),
            str(x.subscription_id),
            str(x.original_receipt_id),
            x.original_receipt_ref,
            str(x.original_revision_id),
            x.original_revision_sequence,
            str(x.infrastructure_ticket_id),
            str(x.reviewed_revision_id),
            x.reviewed_revision_sequence,
            x.membership_token,
            x.resolved_at.isoformat(),
            tuple((str(p.issuance_link_id), str(p.work_order_id)) for p in x.issuances),
            EmptySharedScopeReview.restored_without_issued_field_scope.value
            if not x.issuances
            else None,
        )
        for x in scopes
    )


def _validate_reviews(
    *,
    scopes: tuple[ComplaintResolutionScope, ...],
    reviews: tuple[ReviewedComplaintResolutionScope, ...],
) -> None:
    if len(reviews) != len(scopes) or len(
        {r.complaint_link_id for r in reviews}
    ) != len(reviews):
        raise DomainError(
            code="support.resolution_shared_review_required",
            message="Explicitly review every linked shared incident, restoration and exact issued work scope.",
            retryable=False,
        )
    by_id = {r.complaint_link_id: r for r in reviews}
    for scope in scopes:
        review = by_id.get(scope.complaint_link_id)
        expected_empty = (
            EmptySharedScopeReview.restored_without_issued_field_scope
            if not scope.issuances
            else None
        )
        if review is None or (
            review.reviewed_revision_id,
            review.membership_token,
            review.resolved_at,
            tuple(sorted(review.issuance_link_ids)),
            review.empty_scope_review,
        ) != (
            scope.reviewed_revision_id,
            scope.membership_token,
            scope.resolved_at,
            tuple(sorted(p.issuance_link_id for p in scope.issuances)),
            expected_empty,
        ):
            raise DomainError(
                code="support.resolution_shared_scope_changed",
                message="The reviewed shared incident or issued field scope changed. Reload current evidence and start a new intent.",
                retryable=False,
            )


def _persist(
    db: Session, *, request_id: UUID, scopes: tuple[ComplaintResolutionScope, ...]
) -> None:
    for scope in scopes:
        db.add(
            SupportResolutionSharedIncident(
                request_id=request_id,
                complaint_link_id=scope.complaint_link_id,
                incident_id=scope.incident_id,
                subscription_id=scope.subscription_id,
                original_receipt_id=scope.original_receipt_id,
                original_receipt_ref=scope.original_receipt_ref,
                original_revision_id=scope.original_revision_id,
                original_revision_sequence=scope.original_revision_sequence,
                infrastructure_ticket_id=scope.infrastructure_ticket_id,
                reviewed_revision_id=scope.reviewed_revision_id,
                reviewed_revision_sequence=scope.reviewed_revision_sequence,
                membership_token=scope.membership_token,
                resolved_at=scope.resolved_at,
                empty_scope_review=EmptySharedScopeReview.restored_without_issued_field_scope.value
                if not scope.issuances
                else None,
            )
        )
    db.flush()
    for scope in scopes:
        for proof in scope.issuances:
            db.add(
                SupportResolutionSharedWorkScope(
                    request_id=request_id,
                    complaint_link_id=scope.complaint_link_id,
                    issuance_link_id=proof.issuance_link_id,
                    work_order_id=proof.work_order_id,
                )
            )
    db.flush()


def _matches(
    db: Session, *, request_id: UUID, scopes: tuple[ComplaintResolutionScope, ...]
) -> bool:
    from datetime import UTC

    rows = db.scalars(
        select(SupportResolutionSharedIncident)
        .where(SupportResolutionSharedIncident.request_id == request_id)
        .order_by(SupportResolutionSharedIncident.complaint_link_id)
    ).all()
    stored = []
    for x in rows:
        work = db.scalars(
            select(SupportResolutionSharedWorkScope)
            .where(
                SupportResolutionSharedWorkScope.request_id == request_id,
                SupportResolutionSharedWorkScope.complaint_link_id
                == x.complaint_link_id,
            )
            .order_by(SupportResolutionSharedWorkScope.issuance_link_id)
        ).all()
        resolved = (
            x.resolved_at if x.resolved_at.tzinfo else x.resolved_at.replace(tzinfo=UTC)
        )
        stored.append(
            (
                str(x.complaint_link_id),
                str(x.incident_id),
                str(x.subscription_id),
                str(x.original_receipt_id),
                x.original_receipt_ref,
                str(x.original_revision_id),
                x.original_revision_sequence,
                str(x.infrastructure_ticket_id),
                str(x.reviewed_revision_id),
                x.reviewed_revision_sequence,
                x.membership_token,
                resolved.isoformat(),
                tuple((str(p.issuance_link_id), str(p.work_order_id)) for p in work),
                x.empty_scope_review,
            )
        )
    return tuple(stored) == _material(scopes)
