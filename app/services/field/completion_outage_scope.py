"""Completion-owned exact shared-infrastructure replacement proof participant.

Incident and canonical ticket locks precede Project/WO/head locks. Historical
revocation copies the immutable proof and does not ask issuance eligibility or
restoration state to authorize undoing an existing decision.
"""

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.field_completion_review import (
    CompletionEnrollment,
    CompletionReplacementDecision,
)
from app.models.shared_outage_completion import CompletionReplacementOutageProof
from app.services.domain_errors import DomainError
from app.services.permission_admission import PermissionAdmission
from app.services.topology import outage_completion
from app.services.topology.outage_completion_contracts import (
    OutageCompletionGraphQuery,
    OutageCompletionIssuanceQuery,
    OutageCompletionPurpose,
    OutageCoverageParticipant,
    OutageReplacementCoveragePreviewQuery,
    OutageReplacementCoverageQuery,
    ScopedOutageCoveragePreviewQuery,
)


def _error() -> DomainError:
    return DomainError(
        code="operations.field_completion.outage_replacement_proof_required",
        message="Review exact native incident issuance and full original service coverage before replacing this shared scope.",
    )


def prepare_outage_replacement_graph(db: Session, *, work_order_id: UUID) -> None:
    outage_completion.lock_outage_completion_graph(
        db, query=OutageCompletionGraphQuery(work_order_id)
    )


def review_outage_replacement(
    db: Session,
    *,
    source: CompletionEnrollment,
    replacement: CompletionEnrollment,
    access: PermissionAdmission,
    purpose: str,
) -> None:
    for item in (source, replacement):
        proof = outage_completion.completion_issuance_evidence(
            db,
            query=OutageCompletionIssuanceQuery(
                item.work_order_id,
                access,
                OutageCompletionPurpose.read
                if purpose == "read"
                else OutageCompletionPurpose.review,
            ),
        )
        if (
            proof.infrastructure_ticket_id,
            proof.project_id,
            proof.task_id,
            proof.work_order_public_id,
        ) != (item.origin_ticket_id, item.project_id, item.task_id, item.public_id):
            raise _error()
    # Preview uses the same network proof kernel after fixed completion admission.
    outage_completion.preview_outage_replacement_coverage(
        db,
        query=OutageReplacementCoveragePreviewQuery(
            source.work_order_id,
            replacement.work_order_id,
            access,
            OutageCompletionPurpose.read
            if purpose == "read"
            else OutageCompletionPurpose.review,
        ),
    )


def _values(proof: object) -> dict[str, object]:
    # Typed network outcome is kept at this boundary; ORM storage is this owner.
    from app.services.topology.outage_completion_contracts import (
        OutageReplacementCoverage,
    )

    if not isinstance(proof, OutageReplacementCoverage):
        raise _error()
    a, b = proof.source, proof.replacement
    return dict(
        source_issuance_link_id=a.issuance_link_id,
        replacement_issuance_link_id=b.issuance_link_id,
        incident_id=a.incident_id,
        infrastructure_ticket_id=a.infrastructure_ticket_id,
        revision_id=a.issued_revision_id,
        revision_sequence=a.issued_revision_sequence,
        membership_token=a.membership_token,
        replacement_revision_id=b.issued_revision_id,
        replacement_revision_sequence=b.issued_revision_sequence,
        replacement_membership_token=b.membership_token,
        target_type=a.target_type,
        target_id=a.target_id,
        project_id=a.project_id,
        task_id=a.task_id,
        purpose=a.purpose,
        access_technology=a.access_technology,
    )


def stage_outage_replacement_proof(
    db: Session,
    *,
    decision: CompletionReplacementDecision,
    source: CompletionEnrollment,
    replacement: CompletionEnrollment,
    previous: CompletionReplacementDecision | None,
) -> None:
    if previous is not None:
        old = db.get(CompletionReplacementOutageProof, previous.id)
        if old is None:
            raise _error()
        values = {
            column.name: getattr(old, column.name)
            for column in old.__table__.columns
            if column.name != "decision_id"
        }
    else:
        values = _values(
            outage_completion.stage_outage_replacement_coverage(
                db,
                query=OutageReplacementCoverageQuery(
                    source.work_order_id, replacement.work_order_id
                ),
            )
        )
    db.add(CompletionReplacementOutageProof(decision_id=decision.id, **values))
    db.flush()


def validate_outage_replacement_proof(
    db: Session,
    *,
    decision_id: UUID,
    source: CompletionEnrollment,
    replacement: CompletionEnrollment,
    access: PermissionAdmission | None = None,
    participant: OutageCoverageParticipant = OutageCoverageParticipant.completion_review,
) -> None:
    stored = db.get(
        CompletionReplacementOutageProof, decision_id, populate_existing=True
    )
    if stored is None:
        raise _error()
    query = OutageReplacementCoverageQuery(
        source.work_order_id, replacement.work_order_id
    )
    actual = _values(
        outage_completion.stage_outage_replacement_coverage(db, query=query)
        if access is None
        else outage_completion.preview_scoped_outage_replacement_coverage(
            db,
            query=ScopedOutageCoveragePreviewQuery(
                source.work_order_id, replacement.work_order_id, access, participant
            ),
        )
    )
    if any(getattr(stored, key) != value for key, value in actual.items()):
        raise _error()
