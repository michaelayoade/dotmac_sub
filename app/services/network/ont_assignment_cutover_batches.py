"""Immutable, independently reviewed batches of explicit ONT identity repairs.

This service binds operator-selected repairs to one complete cutover audit and
delegates every proposal/review transition to ``network.ont_assignment_identity``.
It deliberately has no execution operation: approved decisions remain individual
commands owned and revalidated by the identity owner.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.ont_assignment_cutover import (
    OntAssignmentCutoverBatchReview,
    OntAssignmentCutoverProposalBatch,
)
from app.models.ont_assignment_identity import OntAssignmentIdentityDecision
from app.services.network.ont_assignment_cutover import (
    OntAssignmentCutoverAudit,
    OntAssignmentCutoverFinding,
    audit_ont_assignment_cutover,
)
from app.services.network.ont_assignment_identity import (
    ACTIVE_STATUSES,
    OntAssignmentIdentityError,
    approve_assignment_identity_repair,
    decline_assignment_identity_repair,
    preview_assignment_identity_repair,
    propose_assignment_identity_repair,
)

MAX_BATCH_ITEMS = 100
_ACTIONS = {"canonicalize", "deactivate"}
_REVIEW_ACTIONS = {"approve", "decline"}


class OntAssignmentCutoverBatchError(ValueError):
    """Raised when a cutover batch transition is invalid."""


class OntAssignmentCutoverBatchBlocked(OntAssignmentCutoverBatchError):
    """Raised when an exact preview is not safe to persist."""

    def __init__(self, preview: OntAssignmentCutoverBatchPreview) -> None:
        super().__init__("ONT assignment cutover batch preview is blocked")
        self.preview = preview


@dataclass(frozen=True)
class OntAssignmentCutoverBatchPreview:
    expected_report_sha256: str
    current_report_sha256: str
    request_sha256: str
    manifest_sha256: str | None
    manifest_payload: dict[str, object] | None
    blockers: tuple[dict[str, object], ...]
    existing_batch_id: uuid.UUID | None = None

    @property
    def ready(self) -> bool:
        return not self.blockers and self.manifest_sha256 is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "blockers": list(self.blockers),
            "current_report_sha256": self.current_report_sha256,
            "existing_batch_id": (
                str(self.existing_batch_id) if self.existing_batch_id else None
            ),
            "expected_report_sha256": self.expected_report_sha256,
            "manifest_payload": self.manifest_payload,
            "manifest_sha256": self.manifest_sha256,
            "ready": self.ready,
            "request_sha256": self.request_sha256,
        }


@dataclass(frozen=True)
class OntAssignmentCutoverBatchProposalResult:
    batch: OntAssignmentCutoverProposalBatch
    decisions: tuple[OntAssignmentIdentityDecision, ...]
    created: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_id": str(self.batch.id),
            "created": self.created,
            "decision_ids": [str(decision.id) for decision in self.decisions],
            "item_count": self.batch.item_count,
            "manifest_sha256": self.batch.manifest_sha256,
            "report_sha256": self.batch.report_sha256,
            "request_sha256": self.batch.request_sha256,
        }


@dataclass(frozen=True)
class OntAssignmentCutoverBatchReviewResult:
    batch: OntAssignmentCutoverProposalBatch
    review: OntAssignmentCutoverBatchReview
    decisions: tuple[OntAssignmentIdentityDecision, ...]
    created: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.review.action,
            "attestation_sha256": self.review.attestation_sha256,
            "batch_id": str(self.batch.id),
            "created": self.created,
            "decision_ids": [str(decision.id) for decision in self.decisions],
            "decision_statuses": [decision.status for decision in self.decisions],
            "manifest_sha256": self.batch.manifest_sha256,
            "review_id": str(self.review.id),
        }


@dataclass(frozen=True)
class OntAssignmentCutoverBatchEvidence:
    """Validated immutable batch, review, and exact delegated decision rows."""

    batch: OntAssignmentCutoverProposalBatch
    review: OntAssignmentCutoverBatchReview | None
    decisions: tuple[OntAssignmentIdentityDecision, ...]


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _required_text(value: object, field: str, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise OntAssignmentCutoverBatchError(f"{field} is required")
    if len(normalized) > limit:
        raise OntAssignmentCutoverBatchError(
            f"{field} must be at most {limit} characters"
        )
    return normalized


def _sha256(value: object, field: str) -> str:
    normalized = _required_text(value, field, limit=64).lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise OntAssignmentCutoverBatchError(
            f"{field} must be a lowercase-compatible SHA-256 digest"
        )
    return normalized


def _uuid(value: object, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise OntAssignmentCutoverBatchError(f"{field} must be a UUID") from exc


def _optional_uuid(value: object | None, field: str) -> str | None:
    if value in (None, ""):
        return None
    return str(_uuid(value, field))


def _uuid_list(value: object | None, field: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (str, bytes)):
        values: Iterable[object] = [
            part.strip() for part in str(value).split(",") if part.strip()
        ]
    elif isinstance(value, Iterable):
        values = value
    else:
        raise OntAssignmentCutoverBatchError(f"{field} must be a sequence of UUIDs")
    return sorted({str(_uuid(item, field)) for item in values})


def _normalize_items(items: object) -> list[dict[str, object]]:
    if isinstance(items, (str, bytes, Mapping)) or not isinstance(items, Iterable):
        raise OntAssignmentCutoverBatchError("items must be a JSON array")
    raw_items = list(items)
    if not raw_items:
        raise OntAssignmentCutoverBatchError("items must not be empty")
    if len(raw_items) > MAX_BATCH_ITEMS:
        raise OntAssignmentCutoverBatchError(
            f"items must contain at most {MAX_BATCH_ITEMS} repairs"
        )

    normalized: list[dict[str, object]] = []
    for row_number, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, Mapping):
            raise OntAssignmentCutoverBatchError(
                f"items[{row_number}] must be an object"
            )
        action = _required_text(
            raw_item.get("action"), f"items[{row_number}].action", limit=20
        ).lower()
        if action not in _ACTIONS:
            raise OntAssignmentCutoverBatchError(
                f"items[{row_number}].action is unsupported"
            )
        normalized.append(
            {
                "action": action,
                "assignment_id": str(
                    _uuid(
                        raw_item.get("assignment_id"),
                        f"items[{row_number}].assignment_id",
                    )
                ),
                "duplicate_assignment_ids": _uuid_list(
                    raw_item.get("duplicate_assignment_ids"),
                    f"items[{row_number}].duplicate_assignment_ids",
                ),
                "finding_sha256": _sha256(
                    raw_item.get("finding_sha256"),
                    f"items[{row_number}].finding_sha256",
                ),
                "reason": _required_text(
                    raw_item.get("reason"),
                    f"items[{row_number}].reason",
                    limit=4000,
                ),
                "row_number": row_number,
                "target_olt_id": _optional_uuid(
                    raw_item.get("target_olt_id"),
                    f"items[{row_number}].target_olt_id",
                ),
                "target_pon_port_id": _optional_uuid(
                    raw_item.get("target_pon_port_id"),
                    f"items[{row_number}].target_pon_port_id",
                ),
                "target_subscription_id": _optional_uuid(
                    raw_item.get("target_subscription_id"),
                    f"items[{row_number}].target_subscription_id",
                ),
            }
        )
    return normalized


def _request_payload(
    *,
    expected_report_sha256: str,
    normalized_items: list[dict[str, object]],
    proposed_by: str,
    reason: str,
    source_name: str,
) -> dict[str, object]:
    return {
        "expected_report_sha256": expected_report_sha256,
        "items": normalized_items,
        "proposed_by": proposed_by,
        "reason": reason,
        "schema_version": 1,
        "source_name": source_name,
    }


def _existing_batch(
    db: Session, request_sha256: str
) -> OntAssignmentCutoverProposalBatch | None:
    return db.scalar(
        select(OntAssignmentCutoverProposalBatch).where(
            OntAssignmentCutoverProposalBatch.request_sha256 == request_sha256
        )
    )


def _finding_map(
    audit: OntAssignmentCutoverAudit,
) -> dict[str, OntAssignmentCutoverFinding]:
    return {str(finding.assignment_id): finding for finding in audit.findings}


def _manifest_items(payload: Mapping[str, object]) -> list[dict[str, object]]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not all(
        isinstance(item, dict) for item in raw_items
    ):
        raise OntAssignmentCutoverBatchError(
            "stored cutover batch manifest items are invalid"
        )
    return cast(list[dict[str, object]], raw_items)


def _decision_scope(decision: OntAssignmentIdentityDecision) -> set[str]:
    return {
        str(decision.primary_assignment_id),
        *(str(value) for value in decision.duplicate_assignment_ids),
    }


def _active_decision_scopes(
    db: Session,
) -> list[tuple[OntAssignmentIdentityDecision, set[str]]]:
    return [
        (decision, _decision_scope(decision))
        for decision in db.scalars(
            select(OntAssignmentIdentityDecision).where(
                OntAssignmentIdentityDecision.status.in_(ACTIVE_STATUSES)
            )
        )
    ]


def preview_ont_assignment_cutover_batch(
    db: Session,
    *,
    expected_report_sha256: object,
    items: object,
    proposed_by: object,
    reason: object,
    source_name: object = "operator",
) -> OntAssignmentCutoverBatchPreview:
    """Resolve an exact batch manifest without writing any state."""

    expected_report = _sha256(expected_report_sha256, "expected_report_sha256")
    actor = _required_text(proposed_by, "proposed_by", limit=160)
    batch_reason = _required_text(reason, "reason", limit=4000)
    source = _required_text(source_name, "source_name", limit=255)
    normalized_items = _normalize_items(items)
    request_payload = _request_payload(
        expected_report_sha256=expected_report,
        normalized_items=normalized_items,
        proposed_by=actor,
        reason=batch_reason,
        source_name=source,
    )
    request_sha256 = _digest(request_payload)
    existing = _existing_batch(db, request_sha256)
    if existing is not None:
        return OntAssignmentCutoverBatchPreview(
            expected_report_sha256=expected_report,
            current_report_sha256=existing.report_sha256,
            request_sha256=request_sha256,
            manifest_sha256=existing.manifest_sha256,
            manifest_payload=existing.manifest_payload,
            blockers=(),
            existing_batch_id=existing.id,
        )

    audit = audit_ont_assignment_cutover(db)
    blockers: list[dict[str, object]] = []
    if audit.report_sha256 != expected_report:
        blockers.append(
            {
                "code": "cutover_report_changed",
                "current_report_sha256": audit.report_sha256,
                "expected_report_sha256": expected_report,
            }
        )

    finding_by_assignment = _finding_map(audit)
    active_scopes = _active_decision_scopes(db)
    selected_scope: dict[str, int] = {}
    manifest_items: list[dict[str, object]] = []
    seen_primary: set[str] = set()
    for item in normalized_items:
        row_number = cast(int, item["row_number"])
        assignment_id = str(item["assignment_id"])
        if assignment_id in seen_primary:
            blockers.append(
                {
                    "assignment_id": assignment_id,
                    "code": "duplicate_selected_assignment",
                    "row_number": row_number,
                }
            )
            continue
        seen_primary.add(assignment_id)
        finding = finding_by_assignment.get(assignment_id)
        if finding is None:
            blockers.append(
                {
                    "assignment_id": assignment_id,
                    "code": "finding_not_current",
                    "row_number": row_number,
                }
            )
            continue
        finding_dict = finding.to_dict()
        if finding_dict["input_sha256"] != item["finding_sha256"]:
            blockers.append(
                {
                    "assignment_id": assignment_id,
                    "code": "finding_changed",
                    "current_finding_sha256": finding_dict["input_sha256"],
                    "expected_finding_sha256": item["finding_sha256"],
                    "row_number": row_number,
                }
            )
            continue
        try:
            repair = preview_assignment_identity_repair(
                db,
                str(item["action"]),
                assignment_id,
                target_subscription_id=cast(
                    str | uuid.UUID | None, item["target_subscription_id"]
                ),
                target_pon_port_id=cast(
                    str | uuid.UUID | None, item["target_pon_port_id"]
                ),
                target_olt_id=cast(str | uuid.UUID | None, item["target_olt_id"]),
                duplicate_assignment_ids=item["duplicate_assignment_ids"],
            )
        except OntAssignmentIdentityError as exc:
            blockers.append(
                {
                    "assignment_id": assignment_id,
                    "code": "identity_preview_invalid",
                    "detail": str(exc),
                    "row_number": row_number,
                }
            )
            continue

        repair_scope = {
            str(repair.primary_assignment_id),
            *(str(value) for value in repair.duplicate_assignment_ids),
        }
        overlap = {
            value: selected_scope[value]
            for value in repair_scope
            if value in selected_scope
        }
        if overlap:
            blockers.append(
                {
                    "assignment_id": assignment_id,
                    "code": "batch_repair_scope_overlap",
                    "overlapping_rows": sorted(set(overlap.values())),
                    "overlapping_assignment_ids": sorted(overlap),
                    "row_number": row_number,
                }
            )
            continue
        existing_conflicts = [
            str(decision.id)
            for decision, scope in active_scopes
            if repair_scope.intersection(scope)
        ]
        if existing_conflicts:
            blockers.append(
                {
                    "assignment_id": assignment_id,
                    "code": "active_identity_decision_overlap",
                    "decision_ids": sorted(existing_conflicts),
                    "row_number": row_number,
                }
            )
            continue
        for scope_assignment_id in repair_scope:
            selected_scope[scope_assignment_id] = row_number
        manifest_items.append(
            {
                "action": item["action"],
                "assignment_id": assignment_id,
                "finding": finding_dict,
                "finding_sha256": item["finding_sha256"],
                "reason": item["reason"],
                "repair": repair.to_dict(),
                "row_number": row_number,
            }
        )

    if blockers:
        return OntAssignmentCutoverBatchPreview(
            expected_report_sha256=expected_report,
            current_report_sha256=audit.report_sha256,
            request_sha256=request_sha256,
            manifest_sha256=None,
            manifest_payload=None,
            blockers=tuple(blockers),
        )

    manifest_payload: dict[str, object] = {
        "items": manifest_items,
        "proposed_by": actor,
        "reason": batch_reason,
        "report_sha256": audit.report_sha256,
        "request_sha256": request_sha256,
        "schema_version": 1,
        "source_name": source,
    }
    return OntAssignmentCutoverBatchPreview(
        expected_report_sha256=expected_report,
        current_report_sha256=audit.report_sha256,
        request_sha256=request_sha256,
        manifest_sha256=_digest(manifest_payload),
        manifest_payload=manifest_payload,
        blockers=(),
    )


def _batch_decisions(
    db: Session,
    batch: OntAssignmentCutoverProposalBatch,
    *,
    for_update: bool,
) -> tuple[OntAssignmentIdentityDecision, ...]:
    statement = (
        select(OntAssignmentIdentityDecision)
        .where(OntAssignmentIdentityDecision.proposal_batch_id == batch.id)
        .order_by(OntAssignmentIdentityDecision.proposal_batch_row_number)
    )
    if for_update:
        statement = statement.with_for_update()
    decisions = tuple(db.scalars(statement))
    manifest_items = _manifest_items(batch.manifest_payload)
    if len(manifest_items) != batch.item_count:
        raise OntAssignmentCutoverBatchError("stored cutover batch manifest is invalid")
    if len(decisions) != batch.item_count:
        raise OntAssignmentCutoverBatchError(
            "cutover batch decision count does not match its immutable manifest"
        )
    for decision, item in zip(decisions, manifest_items, strict=True):
        if not isinstance(item, dict):
            raise OntAssignmentCutoverBatchError(
                "stored cutover batch manifest item is invalid"
            )
        repair = item.get("repair")
        if not isinstance(repair, dict):
            raise OntAssignmentCutoverBatchError(
                "stored cutover batch repair evidence is invalid"
            )
        expected = {
            "action": item.get("action"),
            "duplicate_assignment_ids": repair.get("duplicate_assignment_ids"),
            "input_sha256": repair.get("input_sha256"),
            "primary_assignment_id": item.get("assignment_id"),
            "proposal_batch_row_number": item.get("row_number"),
            "reason": item.get("reason"),
            "target_olt_id": repair.get("target_olt_id"),
            "target_pon_port_id": repair.get("target_pon_port_id"),
            "target_subscription_id": repair.get("target_subscription_id"),
        }
        actual = {
            "action": decision.action,
            "duplicate_assignment_ids": decision.duplicate_assignment_ids,
            "input_sha256": decision.input_sha256,
            "primary_assignment_id": str(decision.primary_assignment_id),
            "proposal_batch_row_number": decision.proposal_batch_row_number,
            "reason": decision.reason,
            "target_olt_id": (
                str(decision.target_olt_id) if decision.target_olt_id else None
            ),
            "target_pon_port_id": (
                str(decision.target_pon_port_id)
                if decision.target_pon_port_id
                else None
            ),
            "target_subscription_id": (
                str(decision.target_subscription_id)
                if decision.target_subscription_id
                else None
            ),
        }
        if actual != expected or decision.proposed_by != batch.proposed_by:
            raise OntAssignmentCutoverBatchError(
                "cutover batch decision differs from its immutable manifest"
            )
    return decisions


def propose_ont_assignment_cutover_batch(
    db: Session,
    *,
    expected_report_sha256: object,
    items: object,
    proposed_by: object,
    reason: object,
    source_name: object = "operator",
) -> OntAssignmentCutoverBatchProposalResult:
    """Persist one immutable manifest and all delegated proposals atomically."""

    preview = preview_ont_assignment_cutover_batch(
        db,
        expected_report_sha256=expected_report_sha256,
        items=items,
        proposed_by=proposed_by,
        reason=reason,
        source_name=source_name,
    )
    if preview.existing_batch_id is not None:
        batch = _load_batch(db, preview.existing_batch_id, for_update=False)
        return OntAssignmentCutoverBatchProposalResult(
            batch=batch,
            decisions=_batch_decisions(db, batch, for_update=False),
            created=False,
        )
    if not preview.ready or preview.manifest_payload is None:
        raise OntAssignmentCutoverBatchBlocked(preview)

    payload = preview.manifest_payload
    manifest_items = _manifest_items(payload)
    manifest_sha256 = preview.manifest_sha256
    if manifest_sha256 is None:
        raise OntAssignmentCutoverBatchError("ready preview has no manifest digest")
    batch = OntAssignmentCutoverProposalBatch(
        report_sha256=preview.current_report_sha256,
        request_sha256=preview.request_sha256,
        manifest_sha256=manifest_sha256,
        manifest_payload=payload,
        item_count=len(manifest_items),
        source_name=str(payload["source_name"]),
        proposed_by=str(payload["proposed_by"]),
        reason=str(payload["reason"]),
    )
    try:
        decisions: list[OntAssignmentIdentityDecision] = []
        with db.begin_nested():
            db.add(batch)
            db.flush()
            for manifest_item in manifest_items:
                repair = cast(dict[str, object], manifest_item["repair"])
                decisions.append(
                    propose_assignment_identity_repair(
                        db,
                        str(manifest_item["action"]),
                        str(manifest_item["assignment_id"]),
                        proposed_by=batch.proposed_by,
                        reason=str(manifest_item["reason"]),
                        target_subscription_id=cast(
                            str | uuid.UUID | None,
                            repair["target_subscription_id"],
                        ),
                        target_pon_port_id=cast(
                            str | uuid.UUID | None, repair["target_pon_port_id"]
                        ),
                        target_olt_id=cast(
                            str | uuid.UUID | None, repair["target_olt_id"]
                        ),
                        duplicate_assignment_ids=repair["duplicate_assignment_ids"],
                        expected_input_sha256=str(repair["input_sha256"]),
                        proposal_batch_id=batch.id,
                        proposal_batch_row_number=cast(
                            int, manifest_item["row_number"]
                        ),
                        commit=False,
                    )
                )
        db.commit()
        db.refresh(batch)
        for decision in decisions:
            db.refresh(decision)
        return OntAssignmentCutoverBatchProposalResult(
            batch=batch, decisions=tuple(decisions), created=True
        )
    except IntegrityError:
        existing = _existing_batch(db, preview.request_sha256)
        if existing is not None:
            return OntAssignmentCutoverBatchProposalResult(
                batch=existing,
                decisions=_batch_decisions(db, existing, for_update=False),
                created=False,
            )
        raise


def _load_batch(
    db: Session, batch_id: object, *, for_update: bool
) -> OntAssignmentCutoverProposalBatch:
    statement = select(OntAssignmentCutoverProposalBatch).where(
        OntAssignmentCutoverProposalBatch.id == _uuid(batch_id, "batch_id")
    )
    if for_update:
        statement = statement.with_for_update()
    batch = db.scalar(statement)
    if batch is None:
        raise OntAssignmentCutoverBatchError("ONT assignment cutover batch not found")
    return batch


def get_ont_assignment_cutover_batch_evidence(
    db: Session,
    batch_id: object,
    *,
    for_update: bool = False,
) -> OntAssignmentCutoverBatchEvidence:
    """Load and validate one immutable batch boundary for read consumers."""

    batch = _load_batch(db, batch_id, for_update=for_update)
    decisions = _batch_decisions(db, batch, for_update=for_update)
    review_statement = select(OntAssignmentCutoverBatchReview).where(
        OntAssignmentCutoverBatchReview.proposal_batch_id == batch.id
    )
    if for_update:
        review_statement = review_statement.with_for_update()
    review = db.scalar(review_statement)
    return OntAssignmentCutoverBatchEvidence(
        batch=batch,
        review=review,
        decisions=decisions,
    )


def _revalidate_manifest(
    db: Session,
    batch: OntAssignmentCutoverProposalBatch,
    decisions: Sequence[OntAssignmentIdentityDecision],
) -> None:
    audit = audit_ont_assignment_cutover(db)
    if audit.report_sha256 != batch.report_sha256:
        raise OntAssignmentCutoverBatchError(
            "authoritative cutover report changed after batch proposal"
        )
    finding_by_assignment = _finding_map(audit)
    manifest_items = _manifest_items(batch.manifest_payload)
    for decision, manifest_item in zip(decisions, manifest_items, strict=True):
        finding = finding_by_assignment.get(str(decision.primary_assignment_id))
        if finding is None or finding.input_sha256 != manifest_item["finding_sha256"]:
            raise OntAssignmentCutoverBatchError(
                "authoritative cutover finding changed after batch proposal"
            )


def review_ont_assignment_cutover_batch(
    db: Session,
    batch_id: object,
    *,
    expected_manifest_sha256: object,
    action: object,
    reviewed_by: object,
    review_notes: object,
) -> OntAssignmentCutoverBatchReviewResult:
    """Approve or decline every exact proposal in one atomic attestation."""

    expected_manifest = _sha256(expected_manifest_sha256, "expected_manifest_sha256")
    normalized_action = _required_text(action, "action", limit=16).lower()
    if normalized_action not in _REVIEW_ACTIONS:
        raise OntAssignmentCutoverBatchError("action must be approve or decline")
    actor = _required_text(reviewed_by, "reviewed_by", limit=160)
    notes = _required_text(review_notes, "review_notes", limit=4000)
    batch = _load_batch(db, batch_id, for_update=True)
    if batch.manifest_sha256 != expected_manifest:
        raise OntAssignmentCutoverBatchError(
            "cutover batch manifest differs from the expected digest"
        )
    if batch.proposed_by == actor:
        raise OntAssignmentCutoverBatchError(
            "the batch proposer cannot review the same cutover batch"
        )
    attestation_payload = {
        "action": normalized_action,
        "batch_id": str(batch.id),
        "manifest_sha256": batch.manifest_sha256,
        "review_notes": notes,
        "reviewed_by": actor,
        "schema_version": 1,
    }
    attestation_sha256 = _digest(attestation_payload)
    existing_review = db.scalar(
        select(OntAssignmentCutoverBatchReview).where(
            OntAssignmentCutoverBatchReview.proposal_batch_id == batch.id
        )
    )
    if existing_review is not None:
        if existing_review.attestation_sha256 != attestation_sha256:
            raise OntAssignmentCutoverBatchError(
                "cutover batch already has a different review attestation"
            )
        return OntAssignmentCutoverBatchReviewResult(
            batch=batch,
            review=existing_review,
            decisions=_batch_decisions(db, batch, for_update=False),
            created=False,
        )

    decisions = _batch_decisions(db, batch, for_update=True)
    if any(decision.status != "proposed" for decision in decisions):
        raise OntAssignmentCutoverBatchError(
            "every cutover batch decision must still be proposed"
        )
    with db.begin_nested():
        if normalized_action == "approve":
            _revalidate_manifest(db, batch, decisions)
            for decision in decisions:
                approve_assignment_identity_repair(
                    db,
                    decision.id,
                    reviewed_by=actor,
                    review_notes=notes,
                    commit=False,
                )
        else:
            for decision in decisions:
                decline_assignment_identity_repair(
                    db,
                    decision.id,
                    reviewed_by=actor,
                    review_notes=notes,
                    commit=False,
                )
        review = OntAssignmentCutoverBatchReview(
            proposal_batch_id=batch.id,
            batch_manifest_sha256=batch.manifest_sha256,
            action=normalized_action,
            proposed_by=batch.proposed_by,
            reviewed_by=actor,
            review_notes=notes,
            item_count=batch.item_count,
            attestation_sha256=attestation_sha256,
            reviewed_at=datetime.now(UTC),
        )
        db.add(review)
    db.commit()
    db.refresh(review)
    for decision in decisions:
        db.refresh(decision)
    return OntAssignmentCutoverBatchReviewResult(
        batch=batch,
        review=review,
        decisions=decisions,
        created=True,
    )


def inspect_ont_assignment_cutover_batch(
    db: Session, batch_id: object
) -> dict[str, object]:
    evidence = get_ont_assignment_cutover_batch_evidence(db, batch_id)
    batch = evidence.batch
    decisions = evidence.decisions
    review = evidence.review
    return {
        "batch_id": str(batch.id),
        "created_at": batch.created_at.isoformat(),
        "decisions": [
            {
                "action": decision.action,
                "id": str(decision.id),
                "primary_assignment_id": str(decision.primary_assignment_id),
                "row_number": decision.proposal_batch_row_number,
                "status": decision.status,
            }
            for decision in decisions
        ],
        "item_count": batch.item_count,
        "manifest_payload": batch.manifest_payload,
        "manifest_sha256": batch.manifest_sha256,
        "proposed_by": batch.proposed_by,
        "report_sha256": batch.report_sha256,
        "request_sha256": batch.request_sha256,
        "review": (
            {
                "action": review.action,
                "attestation_sha256": review.attestation_sha256,
                "id": str(review.id),
                "review_notes": review.review_notes,
                "reviewed_at": review.reviewed_at.isoformat(),
                "reviewed_by": review.reviewed_by,
            }
            if review
            else None
        ),
        "source_name": batch.source_name,
    }


__all__ = [
    "MAX_BATCH_ITEMS",
    "OntAssignmentCutoverBatchBlocked",
    "OntAssignmentCutoverBatchEvidence",
    "OntAssignmentCutoverBatchError",
    "OntAssignmentCutoverBatchPreview",
    "OntAssignmentCutoverBatchProposalResult",
    "OntAssignmentCutoverBatchReviewResult",
    "get_ont_assignment_cutover_batch_evidence",
    "inspect_ont_assignment_cutover_batch",
    "preview_ont_assignment_cutover_batch",
    "propose_ont_assignment_cutover_batch",
    "review_ont_assignment_cutover_batch",
]
