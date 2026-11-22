"""Read projections and explicit form adapters for ONT identity review."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, NotRequired, TypedDict, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import OntUnit, PonPort
from app.models.ont_assignment_cutover import (
    OntAssignmentCutoverBatchReview,
    OntAssignmentCutoverProposalBatch,
    OntAssignmentCutoverVerificationAttestation,
)
from app.models.ont_assignment_identity import OntAssignmentIdentityDecision
from app.models.ont_topology_observation import OntTopologyObservationEvidence
from app.schemas.status_presentation import StatusTone
from app.services.audit_helpers import resolve_actor_display_names
from app.services.network.ont_assignment_cutover import (
    REASON_LABELS,
    REPAIR_OWNER,
    OntAssignmentCutoverAudit,
    OntAssignmentCutoverFinding,
    audit_ont_assignment_cutover,
)
from app.services.network.ont_assignment_identity import (
    ACTIVE_STATUSES,
    OntAssignmentIdentityError,
    OntAssignmentIdentityPreview,
    active_assignment_identity_conflict_ids,
    page_ont_identity_review_queue,
    preview_assignment_identity_repair,
    propose_assignment_identity_repair,
)
from app.services.ui_contracts import Action

DECISION_STATUSES = ("proposed", "approved", "declined", "applied", "closed")
REVIEW_TABS = ("observations", "candidates", "decisions", "batches")
T = TypeVar("T", covariant=True)


@dataclass(frozen=True, slots=True)
class OntReviewPage(Generic[T]):
    items: tuple[T, ...]
    total: int
    page: int
    per_page: int
    total_pages: int


def _page(total: int, page: int, per_page: int, items: Sequence[T]) -> OntReviewPage[T]:
    total_pages = max(1, (total + per_page - 1) // per_page)
    return OntReviewPage(
        tuple(items), total, min(max(page, 1), total_pages), per_page, total_pages
    )


class OntAssignmentReviewListRow(TypedDict):
    batch: OntAssignmentCutoverProposalBatch
    decisions: list[OntAssignmentIdentityDecision]
    latest_verification: OntAssignmentCutoverVerificationAttestation | None
    review: OntAssignmentCutoverBatchReview | None
    status: str
    proposed_by_name: NotRequired[str]
    reviewed_by_name: NotRequired[str | None]
    verified_by_name: NotRequired[str | None]


class OntTopologyObservationReviewRow(TypedDict):
    evidence: OntTopologyObservationEvidence
    ont_serial_number: str
    proposal_assignment_id: str | None


class OntDecisionReviewRow(TypedDict):
    decision: OntAssignmentIdentityDecision
    ont_serial_number: str
    reviewed_by_name: str | None


class OntReviewTabPageData(TypedDict):
    active_tab: str
    query: str
    selected_status: str
    statuses: tuple[str, ...]
    review_tabs: tuple[str, ...]
    page_meta: OntReviewPage[object]
    observation_reviews: NotRequired[list[OntTopologyObservationReviewRow]]
    cutover_batches: NotRequired[list[OntAssignmentReviewListRow]]
    decisions: NotRequired[list[OntDecisionReviewRow]]
    status_counts: NotRequired[dict[str, int]]
    candidates: NotRequired[list[OntAssignmentIdentityCandidate]]
    cutover_audit: NotRequired[OntAssignmentCutoverAudit]


@dataclass(frozen=True)
class OntAssignmentIdentityCandidate:
    assignment_id: str
    ont_unit_id: str
    ont_serial_number: str
    subscription_id: str | None
    subscriber_id: str | None
    assignment_pon_port_id: str | None
    ont_pon_port_id: str | None
    ont_olt_id: str | None
    reasons: tuple[str, ...]
    related_assignment_ids: tuple[str, ...]
    finding_sha256: str
    repair_owner: str
    review_path: str
    active_decision_id: str | None
    propose_action: Action

    @property
    def reason_labels(self) -> tuple[str, ...]:
        return tuple(REASON_LABELS[reason] for reason in self.reasons)


def _coerce_uuid(value: object, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise OntAssignmentIdentityError(f"{field} must be a UUID") from exc


def _models_by_id(db: Session, model, ids: set[uuid.UUID]) -> dict[uuid.UUID, object]:
    if not ids:
        return {}
    return {row.id: row for row in db.scalars(select(model).where(model.id.in_(ids)))}


def list_assignment_identity_candidates(
    db: Session,
    *,
    query: str | None = None,
    limit: int = 200,
    offset: int = 0,
    cutover_audit: OntAssignmentCutoverAudit | None = None,
) -> list[OntAssignmentIdentityCandidate]:
    """Project the exhaustive cutover audit into the reviewed-repair queue."""

    report = cutover_audit or audit_ont_assignment_cutover(db)
    active_decision_by_assignment = {
        decision.primary_assignment_id: decision.id
        for decision in db.scalars(
            select(OntAssignmentIdentityDecision).where(
                OntAssignmentIdentityDecision.status.in_(ACTIVE_STATUSES)
            )
        )
    }

    normalized_query = str(query or "").strip().lower()
    candidates: list[OntAssignmentIdentityCandidate] = []
    bounded_limit = max(1, limit)
    matched = 0
    for finding in report.findings:
        if normalized_query and normalized_query not in _candidate_searchable(finding):
            continue
        matched += 1
        if matched <= offset:
            continue
        active_decision_id = active_decision_by_assignment.get(finding.assignment_id)
        # Eligibility owned here, not re-derived in the template: a fresh
        # investigation is offered only while no repair decision is already open
        # for this assignment; an active decision blocks a second parallel one.
        propose_action = Action(
            key="propose_repair",
            label="Investigate",
            allowed=active_decision_id is None,
            reason=(
                None
                if active_decision_id is None
                else "An open repair decision already exists for this assignment"
            ),
            permission="network:fiber:write",
            tone=StatusTone.warning,
        )
        candidates.append(
            OntAssignmentIdentityCandidate(
                assignment_id=str(finding.assignment_id),
                ont_unit_id=str(finding.ont_unit_id),
                ont_serial_number=finding.ont_serial_number,
                subscription_id=(
                    str(finding.subscription_id)
                    if finding.subscription_id is not None
                    else None
                ),
                subscriber_id=(
                    str(finding.subscriber_id)
                    if finding.subscriber_id is not None
                    else None
                ),
                assignment_pon_port_id=(
                    str(finding.assignment_pon_port_id)
                    if finding.assignment_pon_port_id is not None
                    else None
                ),
                ont_pon_port_id=(
                    str(finding.ont_pon_port_id)
                    if finding.ont_pon_port_id is not None
                    else None
                ),
                ont_olt_id=(
                    str(finding.ont_olt_id) if finding.ont_olt_id is not None else None
                ),
                reasons=finding.reason_codes,
                related_assignment_ids=tuple(
                    str(value) for value in finding.related_assignment_ids
                ),
                finding_sha256=finding.input_sha256,
                repair_owner=REPAIR_OWNER,
                review_path=finding.review_path,
                active_decision_id=(
                    str(active_decision_id) if active_decision_id else None
                ),
                propose_action=propose_action,
            )
        )
        if len(candidates) >= bounded_limit:
            break
    return candidates


def _candidate_searchable(finding: OntAssignmentCutoverFinding) -> str:
    return " ".join(
        str(value or "")
        for value in (
            finding.assignment_id,
            finding.ont_unit_id,
            finding.ont_serial_number,
            finding.subscription_id,
            finding.subscriber_id,
            finding.assignment_pon_port_id,
            finding.ont_pon_port_id,
            finding.ont_olt_id,
            *finding.related_assignment_ids,
            *finding.reason_codes,
        )
    ).lower()


def list_topology_observation_reviews(
    db: Session,
    *,
    query: str | None = None,
    limit: int = 200,
    evidence_ids: set[uuid.UUID] | None = None,
) -> list[OntTopologyObservationReviewRow]:
    """Project unresolved network observations for manual validation."""

    statement = (
        select(OntTopologyObservationEvidence)
        .where(
            OntTopologyObservationEvidence.latest_outcome.in_(
                ("incomplete", "review_required")
            ),
            OntTopologyObservationEvidence.resolved_at.is_(None),
        )
        .order_by(
            OntTopologyObservationEvidence.last_seen_at.desc(),
            OntTopologyObservationEvidence.id.desc(),
        )
    )
    if evidence_ids is not None:
        statement = statement.where(OntTopologyObservationEvidence.id.in_(evidence_ids))
    evidence_rows = list(db.scalars(statement.limit(max(1, min(limit, 500)))))
    ont_by_id = _models_by_id(
        db, OntUnit, {evidence.ont_unit_id for evidence in evidence_rows}
    )
    normalized_query = str(query or "").strip().lower()
    rows: list[OntTopologyObservationReviewRow] = []
    for evidence in evidence_rows:
        ont = ont_by_id.get(evidence.ont_unit_id)
        searchable = " ".join(
            str(value or "")
            for value in (
                evidence.id,
                evidence.source,
                evidence.evidence_key,
                evidence.ont_unit_id,
                getattr(ont, "serial_number", None),
                evidence.observed_olt_id,
                evidence.observed_pon_port_id,
                evidence.canonical_olt_id,
                evidence.canonical_pon_port_id,
                evidence.latest_reason,
            )
        ).lower()
        if normalized_query and normalized_query not in searchable:
            continue
        rows.append(
            {
                "evidence": evidence,
                "ont_serial_number": getattr(ont, "serial_number", "Unknown ONT"),
                "proposal_assignment_id": (
                    evidence.active_assignment_ids[0]
                    if len(evidence.active_assignment_ids) == 1
                    else None
                ),
            }
        )
    return rows


def list_cutover_proposal_batches(
    db: Session,
    *,
    query: str | None = None,
    limit: int = 100,
    batch_ids: set[uuid.UUID] | None = None,
) -> list[OntAssignmentReviewListRow]:
    """Project immutable cleanup manifests and their delegated decisions."""

    compact = batch_ids is not None
    statement = select(OntAssignmentCutoverProposalBatch).order_by(
        OntAssignmentCutoverProposalBatch.created_at.desc(),
        OntAssignmentCutoverProposalBatch.id.desc(),
    )
    if batch_ids is not None:
        statement = statement.where(OntAssignmentCutoverProposalBatch.id.in_(batch_ids))
    batches = list(db.scalars(statement.limit(max(1, min(limit, 300)))))
    if not batches:
        return []
    batch_ids = {batch.id for batch in batches}
    reviews = {
        review.proposal_batch_id: review
        for review in db.scalars(
            select(OntAssignmentCutoverBatchReview).where(
                OntAssignmentCutoverBatchReview.proposal_batch_id.in_(batch_ids)
            )
        )
    }
    latest_verification_by_batch: dict[
        uuid.UUID, OntAssignmentCutoverVerificationAttestation
    ] = {}
    for attestation in db.scalars(
        select(OntAssignmentCutoverVerificationAttestation)
        .where(
            OntAssignmentCutoverVerificationAttestation.proposal_batch_id.in_(batch_ids)
        )
        .order_by(OntAssignmentCutoverVerificationAttestation.verified_at.desc())
    ):
        latest_verification_by_batch.setdefault(
            attestation.proposal_batch_id, attestation
        )
    decisions_by_batch: dict[uuid.UUID, list[OntAssignmentIdentityDecision]] = {
        batch_id: [] for batch_id in batch_ids
    }
    decision_query = select(OntAssignmentIdentityDecision).where(
        OntAssignmentIdentityDecision.proposal_batch_id.in_(batch_ids)
    )
    if compact:
        ranked_decisions = (
            select(
                OntAssignmentIdentityDecision.id.label("decision_id"),
                func.row_number()
                .over(
                    partition_by=OntAssignmentIdentityDecision.proposal_batch_id,
                    order_by=(
                        OntAssignmentIdentityDecision.proposal_batch_row_number,
                        OntAssignmentIdentityDecision.id,
                    ),
                )
                .label("row_rank"),
            )
            .where(OntAssignmentIdentityDecision.proposal_batch_id.in_(batch_ids))
            .subquery()
        )
        decision_query = decision_query.join(
            ranked_decisions,
            OntAssignmentIdentityDecision.id == ranked_decisions.c.decision_id,
        ).where(ranked_decisions.c.row_rank <= 3)
    for decision in db.scalars(
        decision_query.order_by(
            OntAssignmentIdentityDecision.proposal_batch_id,
            OntAssignmentIdentityDecision.proposal_batch_row_number,
        )
    ):
        if decision.proposal_batch_id is not None:
            decisions_by_batch[decision.proposal_batch_id].append(decision)

    normalized_query = str(query or "").strip().lower()
    rows: list[OntAssignmentReviewListRow] = []
    for batch in batches:
        decisions = decisions_by_batch[batch.id]
        review = reviews.get(batch.id)
        latest_verification = latest_verification_by_batch.get(batch.id)
        searchable = " ".join(
            str(value or "")
            for value in (
                batch.id,
                batch.report_sha256,
                batch.manifest_sha256,
                batch.proposed_by,
                batch.source_name,
                getattr(review, "reviewed_by", None),
                getattr(latest_verification, "verified_by", None),
                getattr(latest_verification, "outcome", None),
                *(decision.id for decision in decisions),
                *(decision.primary_assignment_id for decision in decisions),
            )
        ).lower()
        if normalized_query and normalized_query not in searchable:
            continue
        rows.append(
            {
                "batch": batch,
                "decisions": decisions,
                "latest_verification": latest_verification,
                "review": review,
                "status": (
                    review.action
                    if review is not None
                    else "awaiting independent review"
                ),
            }
        )
    actor_labels = resolve_actor_display_names(
        db,
        {
            actor
            for row in rows
            for actor in (
                row["batch"].proposed_by,
                getattr(row.get("review"), "reviewed_by", None),
                getattr(row.get("latest_verification"), "verified_by", None),
            )
            if actor
        },
    )
    for row in rows:
        review = row.get("review")
        latest_verification = row.get("latest_verification")
        row["proposed_by_name"] = actor_labels.get(
            str(row["batch"].proposed_by), "Former or unknown user"
        )
        row["reviewed_by_name"] = (
            actor_labels.get(str(review.reviewed_by), "Former or unknown user")
            if review
            else None
        )
        row["verified_by_name"] = (
            actor_labels.get(
                str(latest_verification.verified_by), "Former or unknown user"
            )
            if latest_verification
            else None
        )
    return rows


def review_tab_page_data(
    db: Session,
    *,
    tab: str = "observations",
    status: str | None = None,
    query: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> OntReviewTabPageData:
    """Read one review queue, with filters applied before its bounded page."""

    active_tab = tab if tab in REVIEW_TABS else "observations"
    cleaned = str(query or "").strip()[:160]
    size = min(max(per_page, 1), 100)
    empty_page: OntReviewPage[object] = _page(0, 1, size, ())
    result: OntReviewTabPageData = {
        "active_tab": active_tab,
        "query": cleaned,
        "selected_status": status or "active",
        "statuses": ("active", "all", *DECISION_STATUSES),
        "review_tabs": REVIEW_TABS,
        "page_meta": empty_page,
    }
    page_items: tuple[object, ...] = ()
    if active_tab == "candidates":
        audit = audit_ont_assignment_cutover(db)
        matches = [
            finding
            for finding in audit.findings
            if not cleaned or cleaned.lower() in _candidate_searchable(finding)
        ]
        total = len(matches)
        candidate_meta: OntReviewPage[object] = _page(total, page, size, ())
        selected_page = candidate_meta.page
        candidate_rows = list_assignment_identity_candidates(
            db,
            query=cleaned,
            limit=size,
            offset=(selected_page - 1) * size,
            cutover_audit=audit,
        )
        result["candidates"] = candidate_rows
        result["cutover_audit"] = audit
        page_items = tuple(candidate_rows)
    else:
        selection = page_ont_identity_review_queue(
            db,
            tab=active_tab,
            query=cleaned,
            status=status or "active",
            page=page,
            per_page=size,
        )
        total = selection.total
        selected_page = selection.page
        if active_tab == "observations":
            ids = set(selection.ids)
            observation_rows = (
                list_topology_observation_reviews(db, limit=size, evidence_ids=ids)
                if ids
                else []
            )
            result["observation_reviews"] = observation_rows
            page_items = tuple(observation_rows)
        elif active_tab == "batches":
            ids = set(selection.ids)
            batch_rows = (
                list_cutover_proposal_batches(db, limit=size, batch_ids=ids)
                if ids
                else []
            )
            result["cutover_batches"] = batch_rows
            page_items = tuple(batch_rows)
        else:
            decisions = selection.decisions
            labels = resolve_actor_display_names(
                db,
                {
                    actor
                    for decision in decisions
                    for actor in (decision.proposed_by, decision.reviewed_by)
                    if actor
                },
            )
            onts = _models_by_id(
                db, OntUnit, {decision.ont_unit_id for decision in decisions}
            )
            decision_rows: list[OntDecisionReviewRow] = [
                {
                    "decision": decision,
                    "ont_serial_number": getattr(
                        onts.get(decision.ont_unit_id), "serial_number", "Unknown ONT"
                    ),
                    "reviewed_by_name": labels.get(
                        str(decision.reviewed_by), "Former or unknown user"
                    )
                    if decision.reviewed_by
                    else None,
                }
                for decision in decisions
            ]
            result["decisions"] = decision_rows
            page_items = tuple(decision_rows)
            result["status_counts"] = {
                **dict.fromkeys(DECISION_STATUSES, 0),
                **dict(selection.status_counts),
            }
            normalized_status = str(status or "active").strip().lower()
            if normalized_status not in {"active", "all", *DECISION_STATUSES}:
                normalized_status = "active"
            result["selected_status"] = normalized_status
    result["page_meta"] = _page(total, selected_page, size, page_items)
    return result


def decision_detail_page_data(
    db: Session, decision_id: str | uuid.UUID
) -> dict[str, object]:
    decision = db.get(
        OntAssignmentIdentityDecision,
        _coerce_uuid(decision_id, "decision_id"),
    )
    if decision is None:
        raise OntAssignmentIdentityError("assignment identity decision not found")
    ont = db.get(OntUnit, decision.ont_unit_id)
    actor_labels = resolve_actor_display_names(
        db, {decision.proposed_by, decision.reviewed_by}
    )
    current_error: str | None = None
    current_input_sha256: str | None = None
    if decision.status in ACTIVE_STATUSES:
        try:
            current = preview_assignment_identity_repair(
                db,
                decision.action,
                decision.primary_assignment_id,
                target_subscription_id=decision.target_subscription_id,
                target_pon_port_id=decision.target_pon_port_id,
                target_olt_id=decision.target_olt_id,
                duplicate_assignment_ids=decision.duplicate_assignment_ids,
            )
            current_input_sha256 = current.input_sha256
        except OntAssignmentIdentityError as exc:
            current_error = str(exc)
    return {
        "current_error": current_error,
        "current_input_sha256": current_input_sha256,
        "decision": decision,
        "proposed_by_name": actor_labels.get(
            str(decision.proposed_by), "Former or unknown user"
        ),
        "reviewed_by_name": actor_labels.get(
            str(decision.reviewed_by), "Former or unknown user"
        )
        if decision.reviewed_by
        else None,
        "input_is_current": (
            current_error is None and current_input_sha256 == decision.input_sha256
            if decision.status in ACTIVE_STATUSES
            else None
        ),
        "ont": ont,
    }


def preview_from_explicit_form(
    db: Session,
    *,
    action: object,
    primary_assignment_id: object,
    target_subscription_id: object | None = None,
    target_pon_port_id: object | None = None,
) -> OntAssignmentIdentityPreview:
    normalized_action = str(action or "").strip().lower()
    primary_id = _coerce_uuid(primary_assignment_id, "primary_assignment_id")
    if normalized_action == "deactivate":
        return preview_assignment_identity_repair(db, normalized_action, primary_id)
    if normalized_action != "canonicalize":
        raise OntAssignmentIdentityError("unsupported assignment identity action")
    subscription_id = _coerce_uuid(target_subscription_id, "target_subscription_id")
    pon_id = _coerce_uuid(target_pon_port_id, "target_pon_port_id")
    pon = db.get(PonPort, pon_id)
    if pon is None:
        raise OntAssignmentIdentityError("target PON port not found")
    conflicts = active_assignment_identity_conflict_ids(db, primary_id, subscription_id)
    return preview_assignment_identity_repair(
        db,
        normalized_action,
        primary_id,
        target_subscription_id=subscription_id,
        target_pon_port_id=pon.id,
        target_olt_id=pon.olt_id,
        duplicate_assignment_ids=conflicts,
    )


def propose_from_explicit_preview(
    db: Session,
    *,
    action: object,
    primary_assignment_id: object,
    proposed_by: str,
    reason: str,
    expected_input_sha256: str,
    target_subscription_id: object | None = None,
    target_pon_port_id: object | None = None,
) -> OntAssignmentIdentityDecision:
    preview = preview_from_explicit_form(
        db,
        action=action,
        primary_assignment_id=primary_assignment_id,
        target_subscription_id=target_subscription_id,
        target_pon_port_id=target_pon_port_id,
    )
    return propose_assignment_identity_repair(
        db,
        preview.action,
        preview.primary_assignment_id,
        proposed_by=proposed_by,
        reason=reason,
        target_subscription_id=preview.target_subscription_id,
        target_pon_port_id=preview.target_pon_port_id,
        target_olt_id=preview.target_olt_id,
        duplicate_assignment_ids=preview.duplicate_assignment_ids,
        expected_input_sha256=expected_input_sha256,
    )
