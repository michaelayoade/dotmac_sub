"""Network-owned exact shared scope reads and Incident-before-Ticket graph locks."""

from datetime import UTC
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.network_monitoring import (
    OutageIncident,
    OutageIncidentTicketLink,
    OutageIncidentWorkOrderLink,
    OutageScopeRevision,
)
from app.models.outage_complaint_correlation import OutageComplaintCorrelationReceipt
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.services.domain_errors import DomainError
from app.services.owner_commands import owner_command_active
from app.services.staff_permission_admission import revalidate_staff_admission
from app.services.topology.outage_completion_contracts import (
    ComplaintGraphLocks,
    ComplaintGraphLocksQuery,
    ComplaintResolutionScope,
    ComplaintResolutionScopes,
    ComplaintResolutionScopesPreviewQuery,
    ComplaintResolutionScopesQuery,
    CompletionScopeGraphLocksQuery,
    OutageCompletionGraphQuery,
    OutageCompletionIssuance,
    OutageCompletionIssuanceQuery,
    OutageCompletionPurpose,
    OutageCoverageParticipant,
    OutageIssuedTarget,
    OutageReplacementCoverage,
    OutageReplacementCoveragePreviewQuery,
    OutageReplacementCoverageQuery,
    ScopedOutageCoveragePreviewQuery,
)
from app.services.topology.outage_scope_facts import revision_audience_subscription_ids
from app.services.work_order_assignment_contracts import WorkOrderLinkageFence
from app.services.work_order_commands import lock_work_order_linkage
from app.services.work_order_templates import (
    WorkOrderAccessTechnology,
    WorkOrderPurpose,
)


def _error(code: str, message: str) -> DomainError:
    return DomainError(
        code=f"network.outage_completion_evidence.{code}",
        message=message,
        retryable=False,
    )


def _links(
    db: Session, ticket_ids: tuple[UUID, ...]
) -> tuple[OutageIncidentTicketLink, ...]:
    return tuple(
        db.scalars(
            select(OutageIncidentTicketLink)
            .where(
                OutageIncidentTicketLink.ticket_id.in_(ticket_ids),
                OutageIncidentTicketLink.role == "complaint",
            )
            .order_by(OutageIncidentTicketLink.id)
            .execution_options(populate_existing=True)
        ).all()
    )


def lock_complaint_graph(
    db: Session, *, query: ComplaintGraphLocksQuery
) -> ComplaintGraphLocks:
    observed = _links(db, query.ticket_ids)
    observed_binding = tuple(
        (x.id, x.incident_id, x.role, x.ticket_id) for x in observed
    )
    incidents = set(query.incident_ids) | {link.incident_id for link in observed}
    transaction = db.get_transaction()
    prior = db.info.get("outage_completion_graph_locks")
    if prior and prior[0] is transaction and not incidents.issubset(prior[1]):
        raise _error(
            "shared_scope_changed",
            "Incident linkage changed. Retry the entire review before acquiring Ticket locks.",
        )
    for identity in sorted(incidents):
        if (
            db.scalar(
                select(OutageIncident)
                .where(OutageIncident.id == identity)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            is None
        ):
            raise _error(
                "shared_scope_unavailable", "Linked shared incident unavailable."
            )
    infrastructure = tuple(
        db.scalars(
            select(OutageIncidentTicketLink.ticket_id).where(
                OutageIncidentTicketLink.incident_id.in_(incidents),
                OutageIncidentTicketLink.role == "infrastructure",
            )
        ).all()
    )
    tickets = tuple(sorted(set(query.ticket_ids) | set(infrastructure)))
    for identity in tickets:
        lock_work_order_linkage(db, query=WorkOrderLinkageFence(ticket_id=identity))
    for identity in tickets:
        db.scalar(
            select(Ticket)
            .where(Ticket.id == identity)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    actual = _links(db, query.ticket_ids)
    if (
        tuple((x.id, x.incident_id, x.role, x.ticket_id) for x in actual)
        != observed_binding
    ):
        raise _error(
            "shared_scope_changed",
            "Complaint incident linkage changed. Reload and retry the entire review.",
        )
    db.info["outage_completion_graph_locks"] = (
        db.get_transaction(),
        frozenset(incidents)
        | (prior[1] if prior and prior[0] is transaction else frozenset()),
    )
    return ComplaintGraphLocks(tuple(sorted(incidents)), tickets)


def has_complaint_links(db: Session, *, query: ComplaintResolutionScopesQuery) -> bool:
    return bool(_links(db, (query.ticket_id,)))


def _issuance(
    db: Session, link: OutageIncidentWorkOrderLink
) -> OutageCompletionIssuance:
    native = db.scalar(
        select(OutageIncidentTicketLink).where(
            OutageIncidentTicketLink.incident_id == link.incident_id,
            OutageIncidentTicketLink.role == "infrastructure",
        )
    )
    revision = db.get(
        OutageScopeRevision, link.scope_revision_id, populate_existing=True
    )
    job = db.get(WorkOrder, link.work_order_id, populate_existing=True)
    if (
        native is None
        or revision is None
        or job is None
        or revision.incident_id != link.incident_id
        or revision.new_scope_type != link.target_type
        or revision.new_scope_id != link.target_id
        or revision.sequence != link.scope_revision_sequence
        or revision.membership_token != link.membership_token
        or job.origin_ticket_id != native.ticket_id
        or job.subscriber_id is not None
        or job.work_order_kind != "infrastructure"
        or link.target_type not in {x.value for x in OutageIssuedTarget}
    ):
        raise _error(
            "issuance_provenance_unavailable",
            "Shared work requires exact canonical incident issuance provenance; review the issued scope.",
        )
    return OutageCompletionIssuance(
        link.id,
        job.id,
        job.public_id,
        job.title,
        link.incident_id,
        native.ticket_id,
        revision.id,
        revision.sequence,
        link.membership_token,
        OutageIssuedTarget(link.target_type),
        link.target_id,
        job.project_id,
        job.project_task_id,
        WorkOrderPurpose(job.purpose) if job.purpose else None,
        WorkOrderAccessTechnology(job.access_technology)
        if job.access_technology
        else None,
    )


def completion_issuance_evidence(
    db: Session, *, query: OutageCompletionIssuanceQuery
) -> OutageCompletionIssuance:
    permission = (
        "operations:completion-review:read"
        if query.purpose == OutageCompletionPurpose.read
        else "operations:completion-review:write"
    )
    actor = revalidate_staff_admission(
        db,
        admission=query.access,
        permission=permission,
        write=query.purpose == OutageCompletionPurpose.review,
    )
    links = db.scalars(
        select(OutageIncidentWorkOrderLink).where(
            OutageIncidentWorkOrderLink.work_order_id == query.work_order_id
        )
    ).all()
    if len(links) != 1:
        raise _error(
            "issuance_provenance_unavailable",
            "Select work with one exact canonical incident issuance; no legacy scope inference is allowed.",
        )
    lock_complaint_graph(
        db, query=ComplaintGraphLocksQuery((), (links[0].incident_id,))
    )
    result = _issuance(db, links[0])
    if (
        revalidate_staff_admission(
            db,
            admission=query.access,
            permission=permission,
            write=query.purpose == OutageCompletionPurpose.review,
        )
        != actor
    ):
        raise _error("denied", "Current staff identity changed.")
    return result


def stage_complaint_resolution_scopes(
    db: Session, *, query: ComplaintResolutionScopesQuery
) -> ComplaintResolutionScopes:
    if not owner_command_active(db, owner="support.ticket_lifecycle"):
        raise _error("participant_required", "Support lifecycle participant required.")
    return _resolution_scopes(db, query.ticket_id)


def preview_complaint_resolution_scopes(
    db: Session, *, query: ComplaintResolutionScopesPreviewQuery
) -> ComplaintResolutionScopes:
    actor = revalidate_staff_admission(
        db, admission=query.access, permission="support:ticket:update", write=True
    )
    result = _resolution_scopes(db, query.ticket_id)
    if (
        revalidate_staff_admission(
            db, admission=query.access, permission="support:ticket:update", write=True
        )
        != actor
    ):
        raise _error("denied", "Current staff identity changed.")
    return result


def _resolution_scopes(db: Session, ticket_id: UUID) -> ComplaintResolutionScopes:
    lock_complaint_graph(db, query=ComplaintGraphLocksQuery((ticket_id,)))
    ticket = db.get(Ticket, ticket_id, populate_existing=True)
    if ticket is None:
        raise _error("shared_scope_unavailable", "Complaint Ticket unavailable.")
    native_customer = {
        x
        for x in (
            ticket.subscriber_id,
            ticket.customer_account_id,
            ticket.customer_person_id,
        )
        if x is not None
    }
    links = _links(db, (ticket_id,))
    original_proofs = {}
    for link in links:
        receipt_rows = db.scalars(
            select(OutageComplaintCorrelationReceipt)
            .where(
                OutageComplaintCorrelationReceipt.incident_id == link.incident_id,
                OutageComplaintCorrelationReceipt.link_id == link.id,
            )
            .order_by(
                OutageComplaintCorrelationReceipt.created_at,
                OutageComplaintCorrelationReceipt.id,
            )
        ).all()
        receipts = [
            (receipt.id, receipt.ref_id, receipt.subscription_id)
            for receipt in receipt_rows
        ]
        if not receipts or len({identity for _, _, identity in receipts}) != 1:
            raise _error(
                "shared_scope_proof_unavailable",
                "Historical complaint link has no exact native selected-service proof. Review correlation history; do not infer service membership.",
            )
        original_proofs[link.id] = receipts[0]
    subscriptions = {}
    for subscription_id in sorted({proof[2] for proof in original_proofs.values()}):
        subscriptions[subscription_id] = db.scalar(
            select(Subscription)
            .where(Subscription.id == subscription_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    rows = []
    for link in links:
        receipt_id, receipt_ref, subscription_id = original_proofs[link.id]
        subscription = subscriptions[subscription_id]
        if subscription is None or native_customer != {subscription.subscriber_id}:
            raise _error(
                "shared_scope_binding_changed",
                "Selected service no longer belongs to this complaint customer. Review the native customer binding.",
            )
        incident = db.get(OutageIncident, link.incident_id, populate_existing=True)
        current = db.scalar(
            select(OutageScopeRevision)
            .where(OutageScopeRevision.incident_id == link.incident_id)
            .order_by(OutageScopeRevision.sequence.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        original_receipt = db.get(OutageComplaintCorrelationReceipt, receipt_id)
        original = (
            db.get(OutageScopeRevision, original_receipt.original_revision_id)
            if original_receipt
            else None
        )
        native = db.scalar(
            select(OutageIncidentTicketLink).where(
                OutageIncidentTicketLink.incident_id == link.incident_id,
                OutageIncidentTicketLink.role == "infrastructure",
            )
        )
        if (
            incident is None
            or incident.status != "resolved"
            or incident.resolved_at is None
        ):
            raise _error(
                "shared_incident_not_restored",
                "The shared incident must be explicitly restored by Network before requesting customer confirmation.",
            )
        if current is None or original is None or native is None:
            raise _error(
                "shared_scope_proof_unavailable",
                "Canonical infrastructure Ticket and persisted audience revisions are required.",
            )
        # Original immutable audience is authoritative even if current membership later changes.
        if str(subscription_id) not in revision_audience_subscription_ids(original):
            raise _error(
                "shared_scope_proof_unavailable",
                "Original complaint revision does not contain the recorded selected service.",
            )
        issued = []
        for issuance in db.scalars(
            select(OutageIncidentWorkOrderLink)
            .where(OutageIncidentWorkOrderLink.incident_id == link.incident_id)
            .order_by(OutageIncidentWorkOrderLink.id)
        ).all():
            proof = _issuance(db, issuance)
            revision = db.get(OutageScopeRevision, proof.issued_revision_id)
            if revision is not None and str(
                subscription_id
            ) in revision_audience_subscription_ids(revision):
                issued.append(proof)
        resolved_at = (
            incident.resolved_at
            if incident.resolved_at.tzinfo
            else incident.resolved_at.replace(tzinfo=UTC)
        )
        rows.append(
            ComplaintResolutionScope(
                link.id,
                incident.id,
                incident.note or f"Shared incident {incident.id}",
                subscription_id,
                receipt_id,
                receipt_ref or "",
                original.id,
                original.sequence,
                native.ticket_id,
                current.id,
                current.sequence,
                current.membership_token,
                resolved_at,
                tuple(issued),
            )
        )
    return ComplaintResolutionScopes(ticket_id, tuple(rows))


def stage_outage_replacement_coverage(
    db: Session, *, query: OutageReplacementCoverageQuery
) -> OutageReplacementCoverage:
    """Flush-only fixed-owner proof over the two immutable issued audiences."""
    if not (
        owner_command_active(db, owner="operations.field_completion")
        or owner_command_active(db, owner="support.ticket_lifecycle")
    ):
        raise _error(
            "participant_required", "A completion lifecycle command is required."
        )
    return _replacement_coverage(db, query=query)


def _replacement_coverage(
    db: Session, *, query: OutageReplacementCoverageQuery
) -> OutageReplacementCoverage:
    links = tuple(
        db.scalars(
            select(OutageIncidentWorkOrderLink)
            .where(
                OutageIncidentWorkOrderLink.work_order_id.in_(
                    (query.source_work_order_id, query.replacement_work_order_id)
                )
            )
            .execution_options(populate_existing=True)
        ).all()
    )
    by_work = {x.work_order_id: x for x in links}
    if len(links) != 2 or set(by_work) != {
        query.source_work_order_id,
        query.replacement_work_order_id,
    }:
        raise _error(
            "issuance_provenance_unavailable",
            "Both scopes need exact native incident issuance receipts.",
        )
    held = db.info.get("outage_completion_graph_locks")
    if (
        not held
        or held[0] is not db.get_transaction()
        or not {x.incident_id for x in links}.issubset(held[1])
    ):
        raise _error(
            "participant_required",
            "Prepare incident locks before the completion graph.",
        )
    first, second = (
        _issuance(db, by_work[x])
        for x in (query.source_work_order_id, query.replacement_work_order_id)
    )
    fields = (
        "incident_id",
        "infrastructure_ticket_id",
        "target_type",
        "target_id",
        "project_id",
        "task_id",
        "purpose",
        "access_technology",
    )
    if any(getattr(first, x) != getattr(second, x) for x in fields):
        raise _error(
            "replacement_scope_mismatch",
            "Replacement must cover the exact incident, target and work classification.",
        )
    from app.services.topology.outage_scope_facts import (
        revision_audience_subscription_ids,
    )

    source_revision = db.get(OutageScopeRevision, first.issued_revision_id)
    replacement_revision = db.get(OutageScopeRevision, second.issued_revision_id)
    if (
        source_revision is None
        or replacement_revision is None
        or not set(revision_audience_subscription_ids(source_revision)).issubset(
            revision_audience_subscription_ids(replacement_revision)
        )
    ):
        raise _error(
            "replacement_audience_incomplete",
            "Replacement issued audience must include every service in the original issued scope.",
        )
    return OutageReplacementCoverage(first, second)


def lock_outage_completion_graph(
    db: Session, *, query: OutageCompletionGraphQuery
) -> ComplaintGraphLocks:
    links = tuple(
        db.scalars(
            select(OutageIncidentWorkOrderLink).where(
                OutageIncidentWorkOrderLink.work_order_id == query.work_order_id
            )
        ).all()
    )
    if len(links) != 1:
        raise _error(
            "issuance_provenance_unavailable",
            "Review exact native incident issuance before completion.",
        )
    return lock_complaint_graph(
        db, query=ComplaintGraphLocksQuery((), (links[0].incident_id,))
    )


def preview_outage_replacement_coverage(
    db: Session, *, query: OutageReplacementCoveragePreviewQuery
) -> OutageReplacementCoverage:
    permission = (
        "operations:completion-review:read"
        if query.purpose == OutageCompletionPurpose.read
        else "operations:completion-review:write"
    )
    actor = revalidate_staff_admission(
        db,
        admission=query.access,
        permission=permission,
        write=query.purpose == OutageCompletionPurpose.review,
    )
    result = _replacement_coverage(db, query=query)
    if (
        revalidate_staff_admission(
            db,
            admission=query.access,
            permission=permission,
            write=query.purpose == OutageCompletionPurpose.review,
        )
        != actor
    ):
        raise _error("denied", "Current completion staff authority required.")
    return result


def preview_scoped_outage_replacement_coverage(
    db: Session, *, query: ScopedOutageCoveragePreviewQuery
) -> OutageReplacementCoverage:
    permission = {
        OutageCoverageParticipant.support_confirmation: "support:ticket:update",
        OutageCoverageParticipant.project_completion: "project:update",
        OutageCoverageParticipant.project_task_completion: "project:task:write",
        OutageCoverageParticipant.completion_review: "operations:completion-review:write",
    }[query.participant]
    actor = revalidate_staff_admission(
        db, admission=query.access, permission=permission, write=True
    )
    result = _replacement_coverage(db, query=query)
    if (
        revalidate_staff_admission(
            db, admission=query.access, permission=permission, write=True
        )
        != actor
    ):
        raise _error("denied", "Current reviewed staff authority required.")
    return result


def lock_completion_scope_graph(
    db: Session, *, query: CompletionScopeGraphLocksQuery
) -> ComplaintGraphLocks:
    rows = tuple(
        db.scalars(
            select(WorkOrder).where(WorkOrder.id.in_(query.required_work_order_ids))
        ).all()
    )
    incident_ids = tuple(
        db.scalars(
            select(OutageIncidentWorkOrderLink.incident_id).where(
                OutageIncidentWorkOrderLink.work_order_id.in_(
                    query.required_work_order_ids
                )
            )
        ).all()
    )
    return lock_complaint_graph(
        db,
        query=ComplaintGraphLocksQuery(
            tuple(
                sorted(
                    {x.origin_ticket_id for x in rows if x.origin_ticket_id is not None}
                )
            ),
            tuple(sorted(set(incident_ids))),
        ),
    )
