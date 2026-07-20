"""Operator-scale review control for explicit fiber connectivity decisions.

The manifest binds exact staged source content to operator-supplied canonical
endpoint IDs. Geometry is retained as evidence only and is never an endpoint
selector. Every state transition delegates to ``fiber_topology_connectivity``;
canonical mutations remain pending requests owned by ``fiber_asset_changes``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.fiber_topology_connectivity import FiberTopologyConnectivityDecision
from app.models.fiber_topology_connectivity_review import (
    FiberTopologyConnectivityBatchReview,
    FiberTopologyConnectivityProposalBatch,
    FiberTopologyConnectivityRun,
)
from app.services.network.fiber_topology_connectivity import (
    FiberTopologyConnectivityError,
    approve_connectivity_decision,
    decline_connectivity_decision,
    execute_connectivity_decision,
    finalize_connectivity_decision,
    preview_connectivity_decision,
    propose_connectivity_decision,
    validate_connectivity_decision_for_review,
)

MAX_BATCH_ITEMS = 500
MAX_RUN_LIMIT = 100
ACTIONABLE_STATUSES = {
    "execute": ("approved",),
    "reconcile": ("endpoint_change_requested", "segment_change_requested"),
}
OUTCOME_STATUSES = (
    "endpoint_change_requested",
    "segment_change_requested",
    "applied",
    "closed",
    "error",
)


class FiberTopologyConnectivityReviewError(ValueError):
    """Raised when a connectivity batch control request is invalid."""


class FiberTopologyConnectivityProposalBatchBlocked(
    FiberTopologyConnectivityReviewError
):
    def __init__(self, preview: FiberConnectivityProposalBatchPreview):
        self.preview = preview
        super().__init__(
            f"proposal batch has {len(preview.blockers)} blocker(s); nothing was written"
        )


@dataclass(frozen=True)
class FiberConnectivityProposalBatchPreview:
    request_sha256: str
    manifest_sha256: str
    source_name: str
    proposed_by: str
    reason: str
    items: tuple[dict, ...]
    blockers: tuple[dict, ...]
    existing_batch_id: uuid.UUID | None = None

    @property
    def ready(self) -> bool:
        return not self.blockers

    def to_dict(self, *, include_items: bool = True) -> dict:
        payload = {
            "blocker_count": len(self.blockers),
            "blockers": list(self.blockers),
            "existing_batch_id": (
                str(self.existing_batch_id) if self.existing_batch_id else None
            ),
            "item_count": len(self.items),
            "manifest_sha256": self.manifest_sha256,
            "proposed_by": self.proposed_by,
            "ready": self.ready,
            "reason": self.reason,
            "request_sha256": self.request_sha256,
            "source_name": self.source_name,
        }
        if include_items:
            payload["items"] = list(self.items)
        return payload


@dataclass(frozen=True)
class FiberConnectivityProposalBatchResult:
    batch_id: uuid.UUID
    request_sha256: str
    manifest_sha256: str
    decision_ids: tuple[uuid.UUID, ...]
    created: bool

    def to_dict(self) -> dict:
        return {
            "batch_id": str(self.batch_id),
            "created": self.created,
            "decision_ids": [str(value) for value in self.decision_ids],
            "item_count": len(self.decision_ids),
            "manifest_sha256": self.manifest_sha256,
            "request_sha256": self.request_sha256,
        }


@dataclass(frozen=True)
class FiberConnectivityBatchReviewResult:
    review_id: uuid.UUID
    batch_id: uuid.UUID
    batch_manifest_sha256: str
    action: str
    attestation_sha256: str
    decision_ids: tuple[uuid.UUID, ...]
    created: bool

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "attestation_sha256": self.attestation_sha256,
            "batch_id": str(self.batch_id),
            "batch_manifest_sha256": self.batch_manifest_sha256,
            "created": self.created,
            "decision_ids": [str(value) for value in self.decision_ids],
            "item_count": len(self.decision_ids),
            "review_id": str(self.review_id),
        }


@dataclass(frozen=True)
class FiberConnectivityRunResult:
    run_id: uuid.UUID | None
    batch_id: uuid.UUID
    batch_manifest_sha256: str
    run_type: str
    requested_limit: int
    outcomes: tuple[dict, ...]
    remaining_actionable_count: int
    result_sha256: str | None
    created: bool

    @property
    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(OUTCOME_STATUSES, 0)
        for outcome in self.outcomes:
            counts[str(outcome["outcome"])] += 1
        return counts

    def to_dict(self) -> dict:
        return {
            "batch_id": str(self.batch_id),
            "batch_manifest_sha256": self.batch_manifest_sha256,
            "counts": self.counts,
            "created": self.created,
            "outcomes": list(self.outcomes),
            "remaining_actionable_count": self.remaining_actionable_count,
            "requested_limit": self.requested_limit,
            "result_sha256": self.result_sha256,
            "run_id": str(self.run_id) if self.run_id else None,
            "run_type": self.run_type,
            "scanned_count": len(self.outcomes),
        }


def _required_text(value: object, field: str, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise FiberTopologyConnectivityReviewError(f"{field} is required")
    if len(normalized) > limit:
        raise FiberTopologyConnectivityReviewError(
            f"{field} exceeds the {limit}-character limit"
        )
    return normalized


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _optional_int(value: object, field: str, row_number: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise FiberTopologyConnectivityReviewError(
            f"row {row_number} {field} must be an integer"
        )
    return value


def _optional_float(value: object, field: str, row_number: int) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FiberTopologyConnectivityReviewError(
            f"row {row_number} {field} must be a number"
        )
    return float(value)


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalize_batch_request(
    items: Sequence[Mapping[str, Any]],
    *,
    proposed_by: str,
    reason: str,
    source_name: str,
) -> tuple[dict, str, str, str]:
    actor = _required_text(proposed_by, "proposed_by", limit=160)
    normalized_reason = _required_text(reason, "reason", limit=4000)
    normalized_source = _required_text(source_name, "source_name", limit=255)
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise FiberTopologyConnectivityReviewError("items must be a JSON array")
    if len(items) < 1 or len(items) > MAX_BATCH_ITEMS:
        raise FiberTopologyConnectivityReviewError(
            f"items must contain between 1 and {MAX_BATCH_ITEMS} rows"
        )
    normalized_items: list[dict] = []
    for row_number, raw in enumerate(items, start=1):
        if not isinstance(raw, Mapping):
            raise FiberTopologyConnectivityReviewError(
                f"row {row_number} must be a JSON object"
            )
        item_reason = _optional_text(raw.get("reason")) or normalized_reason
        normalized_items.append(
            {
                "action": _optional_text(raw.get("action")),
                "cable_type": _optional_text(raw.get("cable_type")),
                "end_endpoint_ref_id": _optional_text(raw.get("end_endpoint_ref_id")),
                "end_endpoint_type": _optional_text(raw.get("end_endpoint_type")),
                "expected_feature_content_sha256": _optional_text(
                    raw.get("expected_feature_content_sha256")
                ),
                "fiber_count": _optional_int(
                    raw.get("fiber_count"), "fiber_count", row_number
                ),
                "length_m": _optional_float(
                    raw.get("length_m"), "length_m", row_number
                ),
                "reason": item_reason,
                "row_number": row_number,
                "segment_type": _optional_text(raw.get("segment_type"))
                or "distribution",
                "staged_feature_id": _optional_text(raw.get("staged_feature_id")),
                "start_endpoint_ref_id": _optional_text(
                    raw.get("start_endpoint_ref_id")
                ),
                "start_endpoint_type": _optional_text(raw.get("start_endpoint_type")),
                "target_segment_id": _optional_text(raw.get("target_segment_id")),
            }
        )
    request_payload = {
        "items": normalized_items,
        "proposed_by": actor,
        "reason": normalized_reason,
        "schema_version": 1,
        "source_name": normalized_source,
    }
    return request_payload, actor, normalized_reason, normalized_source


def _preview_from_existing_batch(
    batch: FiberTopologyConnectivityProposalBatch,
) -> FiberConnectivityProposalBatchPreview:
    return FiberConnectivityProposalBatchPreview(
        request_sha256=batch.request_sha256,
        manifest_sha256=batch.manifest_sha256,
        source_name=batch.source_name,
        proposed_by=batch.proposed_by,
        reason=batch.reason,
        items=tuple(batch.manifest_payload.get("items") or ()),
        blockers=(),
        existing_batch_id=batch.id,
    )


def _assert_explicit_endpoints(
    item: Mapping[str, Any], resolved: Mapping[str, Any]
) -> None:
    action = str(item.get("action") or "").strip().lower()
    endpoint_fields = (
        "start_endpoint_type",
        "start_endpoint_ref_id",
        "end_endpoint_type",
        "end_endpoint_ref_id",
    )
    if action in {"create", "link_existing"}:
        if any(not item.get(field) for field in endpoint_fields):
            raise FiberTopologyConnectivityReviewError(
                "create and link_existing rows require all explicit endpoint IDs"
            )
        for field in endpoint_fields:
            if str(item[field]) != str(resolved[field]):
                raise FiberTopologyConnectivityReviewError(
                    f"explicit {field} does not match the canonical target"
                )
    elif action == "reject" and any(item.get(field) for field in endpoint_fields):
        raise FiberTopologyConnectivityReviewError(
            "reject rows cannot specify endpoints"
        )


def _assert_requested_endpoint_presence(item: Mapping[str, Any]) -> None:
    action = str(item.get("action") or "").strip().lower()
    endpoint_fields = (
        "start_endpoint_type",
        "start_endpoint_ref_id",
        "end_endpoint_type",
        "end_endpoint_ref_id",
    )
    if action in {"create", "link_existing"} and any(
        not item.get(field) for field in endpoint_fields
    ):
        raise FiberTopologyConnectivityReviewError(
            "create and link_existing rows require all explicit endpoint IDs"
        )
    if action == "reject" and any(item.get(field) for field in endpoint_fields):
        raise FiberTopologyConnectivityReviewError(
            "reject rows cannot specify endpoints"
        )


def preview_connectivity_proposal_batch(
    db: Session,
    items: Sequence[Mapping[str, Any]],
    *,
    proposed_by: str,
    reason: str,
    source_name: str = "operator-manifest",
) -> FiberConnectivityProposalBatchPreview:
    request_payload, actor, normalized_reason, normalized_source = (
        _normalize_batch_request(
            items,
            proposed_by=proposed_by,
            reason=reason,
            source_name=source_name,
        )
    )
    request_sha256 = _digest(request_payload)
    existing = db.scalar(
        select(FiberTopologyConnectivityProposalBatch).where(
            FiberTopologyConnectivityProposalBatch.request_sha256 == request_sha256
        )
    )
    if existing:
        return _preview_from_existing_batch(existing)

    resolved_items: list[dict] = []
    blockers: list[dict] = []
    seen_features: set[str] = set()
    for item in request_payload["items"]:
        row_number = int(item["row_number"])
        feature_id = str(item.get("staged_feature_id") or "")
        if feature_id in seen_features and feature_id:
            blockers.append(
                {
                    "code": "duplicate_staged_feature",
                    "message": "staged path appears more than once in the batch",
                    "row_number": row_number,
                    "staged_feature_id": feature_id,
                }
            )
            resolved_items.append(dict(item))
            continue
        seen_features.add(feature_id)
        try:
            _assert_requested_endpoint_presence(item)
            expected_content = _required_text(
                item.get("expected_feature_content_sha256"),
                "expected_feature_content_sha256",
                limit=64,
            )
            preview = preview_connectivity_decision(
                db,
                feature_id,
                str(item.get("action") or ""),
                proposed_by=actor,
                reason=str(item["reason"]),
                start_endpoint_type=item.get("start_endpoint_type"),
                start_endpoint_ref_id=item.get("start_endpoint_ref_id"),
                end_endpoint_type=item.get("end_endpoint_type"),
                end_endpoint_ref_id=item.get("end_endpoint_ref_id"),
                segment_type=str(item["segment_type"]),
                cable_type=item.get("cable_type"),
                fiber_count=item.get("fiber_count"),
                length_m=item.get("length_m"),
                target_segment_id=item.get("target_segment_id"),
                expected_feature_content_sha256=expected_content,
                require_new=True,
            )
            resolved = preview.to_manifest_dict(row_number=row_number)
            _assert_explicit_endpoints(item, resolved)
            resolved_items.append(resolved)
        except (
            FiberTopologyConnectivityError,
            FiberTopologyConnectivityReviewError,
        ) as exc:
            blockers.append(
                {
                    "code": "connectivity_decision_blocked",
                    "message": str(exc),
                    "row_number": row_number,
                    "staged_feature_id": feature_id or None,
                }
            )
            resolved_items.append(dict(item))

    manifest_payload = {
        "items": resolved_items,
        "proposed_by": actor,
        "reason": normalized_reason,
        "request_sha256": request_sha256,
        "schema_version": 1,
        "source_name": normalized_source,
    }
    return FiberConnectivityProposalBatchPreview(
        request_sha256=request_sha256,
        manifest_sha256=_digest(manifest_payload),
        source_name=normalized_source,
        proposed_by=actor,
        reason=normalized_reason,
        items=tuple(resolved_items),
        blockers=tuple(blockers),
    )


def propose_connectivity_batch(
    db: Session,
    items: Sequence[Mapping[str, Any]],
    *,
    proposed_by: str,
    reason: str,
    source_name: str = "operator-manifest",
) -> FiberConnectivityProposalBatchResult:
    preview = preview_connectivity_proposal_batch(
        db,
        items,
        proposed_by=proposed_by,
        reason=reason,
        source_name=source_name,
    )
    if preview.existing_batch_id:
        decision_ids = tuple(
            db.scalars(
                select(FiberTopologyConnectivityDecision.id)
                .where(
                    FiberTopologyConnectivityDecision.proposal_batch_id
                    == preview.existing_batch_id
                )
                .order_by(FiberTopologyConnectivityDecision.proposal_batch_row_number)
            ).all()
        )
        return FiberConnectivityProposalBatchResult(
            batch_id=preview.existing_batch_id,
            request_sha256=preview.request_sha256,
            manifest_sha256=preview.manifest_sha256,
            decision_ids=decision_ids,
            created=False,
        )
    if not preview.ready:
        raise FiberTopologyConnectivityProposalBatchBlocked(preview)

    manifest_payload = {
        "items": list(preview.items),
        "proposed_by": preview.proposed_by,
        "reason": preview.reason,
        "request_sha256": preview.request_sha256,
        "schema_version": 1,
        "source_name": preview.source_name,
    }
    batch = FiberTopologyConnectivityProposalBatch(
        manifest_sha256=preview.manifest_sha256,
        request_sha256=preview.request_sha256,
        manifest_payload=manifest_payload,
        item_count=len(preview.items),
        source_name=preview.source_name,
        proposed_by=preview.proposed_by,
        reason=preview.reason,
    )
    db.add(batch)
    try:
        db.flush()
        decisions: list[FiberTopologyConnectivityDecision] = []
        for item in preview.items:
            decisions.append(
                propose_connectivity_decision(
                    db,
                    str(item["staged_feature_id"]),
                    str(item["action"]),
                    proposed_by=preview.proposed_by,
                    reason=str(item["reason"]),
                    start_endpoint_type=item.get("start_endpoint_type"),
                    start_endpoint_ref_id=item.get("start_endpoint_ref_id"),
                    end_endpoint_type=item.get("end_endpoint_type"),
                    end_endpoint_ref_id=item.get("end_endpoint_ref_id"),
                    segment_type=str(item.get("segment_type") or "distribution"),
                    cable_type=item.get("cable_type"),
                    fiber_count=item.get("fiber_count"),
                    length_m=item.get("length_m"),
                    target_segment_id=item.get("target_segment_id"),
                    expected_feature_content_sha256=str(item["feature_content_sha256"]),
                    proposal_batch_id=batch.id,
                    proposal_batch_row_number=int(item["row_number"]),
                    commit=False,
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return FiberConnectivityProposalBatchResult(
        batch_id=batch.id,
        request_sha256=preview.request_sha256,
        manifest_sha256=preview.manifest_sha256,
        decision_ids=tuple(decision.id for decision in decisions),
        created=True,
    )


def _coerce_batch_id(value: str | uuid.UUID) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise FiberTopologyConnectivityReviewError("batch_id must be a UUID") from exc


def _load_batch(
    db: Session, batch_id: str | uuid.UUID, *, for_update: bool = False
) -> FiberTopologyConnectivityProposalBatch:
    statement = select(FiberTopologyConnectivityProposalBatch).where(
        FiberTopologyConnectivityProposalBatch.id == _coerce_batch_id(batch_id)
    )
    if for_update:
        statement = statement.with_for_update()
    batch = db.scalar(statement)
    if batch is None:
        raise FiberTopologyConnectivityReviewError(
            "connectivity proposal batch not found"
        )
    return batch


def _expected_manifest(value: str) -> str:
    digest = _required_text(value, "expected_manifest_sha256", limit=64)
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise FiberTopologyConnectivityReviewError(
            "expected_manifest_sha256 must be a lowercase SHA-256 digest"
        )
    return digest


def _decision_manifest_item(
    decision: FiberTopologyConnectivityDecision, row_number: int
) -> dict:
    return {
        "action": decision.action,
        "cable_type": decision.cable_type,
        "decision_sha256": decision.decision_sha256,
        "end_endpoint_ref_id": (
            str(decision.end_endpoint_ref_id) if decision.end_endpoint_ref_id else None
        ),
        "end_endpoint_type": decision.end_endpoint_type,
        "feature_content_sha256": decision.feature_content_sha256,
        "fiber_count": decision.fiber_count,
        "length_m": decision.length_m,
        "proposed_by": decision.proposed_by,
        "reason": decision.reason,
        "row_number": row_number,
        "segment_type": decision.segment_type,
        "source_asset_type": decision.source_asset_type,
        "source_external_id": decision.source_external_id,
        "source_system": decision.source_system,
        "staged_feature_id": str(decision.staged_feature_id),
        "start_endpoint_ref_id": (
            str(decision.start_endpoint_ref_id)
            if decision.start_endpoint_ref_id
            else None
        ),
        "start_endpoint_type": decision.start_endpoint_type,
        "target_segment_id": (
            str(decision.target_segment_id) if decision.target_segment_id else None
        ),
    }


def _batch_decisions(
    db: Session,
    batch: FiberTopologyConnectivityProposalBatch,
    *,
    for_update: bool = False,
) -> list[FiberTopologyConnectivityDecision]:
    statement = (
        select(FiberTopologyConnectivityDecision)
        .where(FiberTopologyConnectivityDecision.proposal_batch_id == batch.id)
        .order_by(FiberTopologyConnectivityDecision.proposal_batch_row_number)
    )
    if for_update:
        statement = statement.with_for_update()
    decisions = list(db.scalars(statement).all())
    manifest_items = list(batch.manifest_payload.get("items") or ())
    if len(decisions) != batch.item_count or len(manifest_items) != batch.item_count:
        raise FiberTopologyConnectivityReviewError(
            "proposal batch decision count does not match its immutable manifest"
        )
    for row_number, (decision, item) in enumerate(
        zip(decisions, manifest_items, strict=True), start=1
    ):
        if (
            item != _decision_manifest_item(decision, row_number)
            or decision.proposal_batch_row_number != row_number
        ):
            raise FiberTopologyConnectivityReviewError(
                f"proposal batch row {row_number} does not match its manifest"
            )
    return decisions


def attest_connectivity_batch(
    db: Session,
    batch_id: str | uuid.UUID,
    *,
    expected_manifest_sha256: str,
    action: str,
    reviewed_by: str,
    review_notes: str,
) -> FiberConnectivityBatchReviewResult:
    actor = _required_text(reviewed_by, "reviewed_by", limit=160)
    notes = _required_text(review_notes, "review_notes", limit=4000)
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"approve", "decline"}:
        raise FiberTopologyConnectivityReviewError("action must be approve or decline")
    expected_manifest = _expected_manifest(expected_manifest_sha256)
    batch = _load_batch(db, batch_id, for_update=True)
    if batch.manifest_sha256 != expected_manifest:
        raise FiberTopologyConnectivityReviewError(
            "expected manifest does not match the proposal batch"
        )
    attestation_payload = {
        "action": normalized_action,
        "batch_id": str(batch.id),
        "batch_manifest_sha256": batch.manifest_sha256,
        "item_count": batch.item_count,
        "proposed_by": batch.proposed_by,
        "review_notes": notes,
        "reviewed_by": actor,
        "schema_version": 1,
    }
    attestation_sha256 = _digest(attestation_payload)
    existing = db.scalar(
        select(FiberTopologyConnectivityBatchReview).where(
            FiberTopologyConnectivityBatchReview.proposal_batch_id == batch.id
        )
    )
    if existing:
        if existing.attestation_sha256 != attestation_sha256:
            raise FiberTopologyConnectivityReviewError(
                "proposal batch already has a different review attestation"
            )
        return FiberConnectivityBatchReviewResult(
            review_id=existing.id,
            batch_id=batch.id,
            batch_manifest_sha256=batch.manifest_sha256,
            action=existing.action,
            attestation_sha256=existing.attestation_sha256,
            decision_ids=tuple(decision.id for decision in _batch_decisions(db, batch)),
            created=False,
        )
    if batch.proposed_by == actor:
        raise FiberTopologyConnectivityReviewError(
            "the batch proposer cannot attest the same proposal batch"
        )
    decisions = _batch_decisions(db, batch, for_update=True)
    if any(decision.status != "proposed" for decision in decisions):
        raise FiberTopologyConnectivityReviewError(
            "every decision must still be proposed; batch review wrote nothing"
        )
    try:
        with db.begin_nested():
            if normalized_action == "approve":
                for decision in decisions:
                    validate_connectivity_decision_for_review(db, decision.id)
                for decision in decisions:
                    approve_connectivity_decision(
                        db,
                        decision.id,
                        reviewed_by=actor,
                        review_notes=notes,
                        commit=False,
                    )
            else:
                for decision in decisions:
                    decline_connectivity_decision(
                        db,
                        decision.id,
                        reviewed_by=actor,
                        review_notes=notes,
                        commit=False,
                    )
            review = FiberTopologyConnectivityBatchReview(
                proposal_batch_id=batch.id,
                batch_manifest_sha256=batch.manifest_sha256,
                action=normalized_action,
                proposed_by=batch.proposed_by,
                reviewed_by=actor,
                review_notes=notes,
                item_count=batch.item_count,
                attestation_sha256=attestation_sha256,
            )
            db.add(review)
            db.flush()
    except (FiberTopologyConnectivityError, FiberTopologyConnectivityReviewError):
        raise
    except Exception:
        db.rollback()
        raise
    db.commit()
    db.refresh(review)
    return FiberConnectivityBatchReviewResult(
        review_id=review.id,
        batch_id=batch.id,
        batch_manifest_sha256=batch.manifest_sha256,
        action=normalized_action,
        attestation_sha256=attestation_sha256,
        decision_ids=tuple(decision.id for decision in decisions),
        created=True,
    )


def _count_actionable(db: Session, batch_id: uuid.UUID, run_type: str) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(FiberTopologyConnectivityDecision)
            .where(
                FiberTopologyConnectivityDecision.proposal_batch_id == batch_id,
                FiberTopologyConnectivityDecision.status.in_(
                    ACTIONABLE_STATUSES[run_type]
                ),
            )
        )
        or 0
    )


def _run_connectivity_batch(
    db: Session,
    batch_id: str | uuid.UUID,
    *,
    expected_manifest_sha256: str,
    actor_value: str,
    limit: int,
    run_type: str,
) -> FiberConnectivityRunResult:
    actor = _required_text(actor_value, "actor", limit=160)
    expected_manifest = _expected_manifest(expected_manifest_sha256)
    if limit < 1 or limit > MAX_RUN_LIMIT:
        raise FiberTopologyConnectivityReviewError(
            f"limit must be between 1 and {MAX_RUN_LIMIT}"
        )
    batch = _load_batch(db, batch_id)
    if batch.manifest_sha256 != expected_manifest:
        raise FiberTopologyConnectivityReviewError(
            "expected manifest does not match the proposal batch"
        )
    review = db.scalar(
        select(FiberTopologyConnectivityBatchReview)
        .where(FiberTopologyConnectivityBatchReview.proposal_batch_id == batch.id)
        .with_for_update()
    )
    if review is None or review.action != "approve":
        raise FiberTopologyConnectivityReviewError(
            "proposal batch requires an approving review attestation"
        )
    if review.batch_manifest_sha256 != batch.manifest_sha256:
        raise FiberTopologyConnectivityReviewError(
            "review attestation does not match the proposal-batch manifest"
        )
    _batch_decisions(db, batch)
    decision_ids = tuple(
        db.scalars(
            select(FiberTopologyConnectivityDecision.id)
            .where(
                FiberTopologyConnectivityDecision.proposal_batch_id == batch.id,
                FiberTopologyConnectivityDecision.status.in_(
                    ACTIONABLE_STATUSES[run_type]
                ),
            )
            .order_by(FiberTopologyConnectivityDecision.proposal_batch_row_number)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
    )
    if not decision_ids:
        return FiberConnectivityRunResult(
            run_id=None,
            batch_id=batch.id,
            batch_manifest_sha256=batch.manifest_sha256,
            run_type=run_type,
            requested_limit=limit,
            outcomes=(),
            remaining_actionable_count=_count_actionable(db, batch.id, run_type),
            result_sha256=None,
            created=False,
        )

    outcomes: list[dict] = []
    try:
        for decision_id in decision_ids:
            try:
                with db.begin_nested():
                    if run_type == "execute":
                        decision = execute_connectivity_decision(
                            db, decision_id, executed_by=actor, commit=False
                        )
                    else:
                        decision = finalize_connectivity_decision(
                            db, decision_id, finalized_by=actor, commit=False
                        )
                outcomes.append(
                    {"decision_id": str(decision_id), "outcome": decision.status}
                )
            except FiberTopologyConnectivityError as exc:
                outcomes.append(
                    {
                        "decision_id": str(decision_id),
                        "message": str(exc),
                        "outcome": "error",
                    }
                )
        remaining = _count_actionable(db, batch.id, run_type)
        run_id = uuid.uuid4()
        result_payload = {
            "batch_id": str(batch.id),
            "batch_manifest_sha256": batch.manifest_sha256,
            "executed_by": actor,
            "outcomes": outcomes,
            "remaining_actionable_count": remaining,
            "requested_limit": limit,
            "run_id": str(run_id),
            "run_type": run_type,
            "schema_version": 1,
        }
        result_sha256 = _digest(result_payload)
        counts = dict.fromkeys(OUTCOME_STATUSES, 0)
        for outcome in outcomes:
            counts[str(outcome["outcome"])] += 1
        run = FiberTopologyConnectivityRun(
            id=run_id,
            proposal_batch_id=batch.id,
            batch_review_id=review.id,
            batch_manifest_sha256=batch.manifest_sha256,
            run_type=run_type,
            executed_by=actor,
            requested_limit=limit,
            scanned_count=len(outcomes),
            endpoint_pending_count=counts["endpoint_change_requested"],
            segment_pending_count=counts["segment_change_requested"],
            applied_count=counts["applied"],
            closed_count=counts["closed"],
            error_count=counts["error"],
            remaining_actionable_count=remaining,
            result_payload=result_payload,
            result_sha256=result_sha256,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
    except Exception:
        db.rollback()
        raise
    return FiberConnectivityRunResult(
        run_id=run.id,
        batch_id=batch.id,
        batch_manifest_sha256=batch.manifest_sha256,
        run_type=run_type,
        requested_limit=limit,
        outcomes=tuple(outcomes),
        remaining_actionable_count=remaining,
        result_sha256=run.result_sha256,
        created=True,
    )


def execute_connectivity_batch(
    db: Session,
    batch_id: str | uuid.UUID,
    *,
    expected_manifest_sha256: str,
    executed_by: str,
    limit: int = 50,
) -> FiberConnectivityRunResult:
    return _run_connectivity_batch(
        db,
        batch_id,
        expected_manifest_sha256=expected_manifest_sha256,
        actor_value=executed_by,
        limit=limit,
        run_type="execute",
    )


def reconcile_connectivity_batch(
    db: Session,
    batch_id: str | uuid.UUID,
    *,
    expected_manifest_sha256: str,
    finalized_by: str,
    limit: int = 50,
) -> FiberConnectivityRunResult:
    return _run_connectivity_batch(
        db,
        batch_id,
        expected_manifest_sha256=expected_manifest_sha256,
        actor_value=finalized_by,
        limit=limit,
        run_type="reconcile",
    )


def inspect_connectivity_batch(db: Session, batch_id: str | uuid.UUID) -> dict:
    """Return the immutable manifest and read-only control evidence."""

    batch = _load_batch(db, batch_id)
    decisions = _batch_decisions(db, batch)
    status_counts: dict[str, int] = {}
    for decision in decisions:
        status_counts[decision.status] = status_counts.get(decision.status, 0) + 1
    review = db.scalar(
        select(FiberTopologyConnectivityBatchReview).where(
            FiberTopologyConnectivityBatchReview.proposal_batch_id == batch.id
        )
    )
    runs = db.scalars(
        select(FiberTopologyConnectivityRun)
        .where(FiberTopologyConnectivityRun.proposal_batch_id == batch.id)
        .order_by(FiberTopologyConnectivityRun.executed_at)
    ).all()
    return {
        "batch_id": str(batch.id),
        "created_at": batch.created_at.isoformat(),
        "decision_status_counts": status_counts,
        "item_count": batch.item_count,
        "manifest_payload": batch.manifest_payload,
        "manifest_sha256": batch.manifest_sha256,
        "proposed_by": batch.proposed_by,
        "review": (
            {
                "action": review.action,
                "attestation_sha256": review.attestation_sha256,
                "review_id": str(review.id),
                "review_notes": review.review_notes,
                "reviewed_at": review.reviewed_at.isoformat(),
                "reviewed_by": review.reviewed_by,
            }
            if review
            else None
        ),
        "runs": [
            {
                "counts": {
                    "applied": run.applied_count,
                    "closed": run.closed_count,
                    "endpoint_change_requested": run.endpoint_pending_count,
                    "error": run.error_count,
                    "segment_change_requested": run.segment_pending_count,
                },
                "executed_at": run.executed_at.isoformat(),
                "executed_by": run.executed_by,
                "remaining_actionable_count": run.remaining_actionable_count,
                "requested_limit": run.requested_limit,
                "result_sha256": run.result_sha256,
                "run_id": str(run.id),
                "run_type": run.run_type,
                "scanned_count": run.scanned_count,
            }
            for run in runs
        ],
        "source_name": batch.source_name,
    }


__all__ = [
    "FiberConnectivityBatchReviewResult",
    "FiberConnectivityProposalBatchPreview",
    "FiberConnectivityProposalBatchResult",
    "FiberConnectivityRunResult",
    "FiberTopologyConnectivityProposalBatchBlocked",
    "FiberTopologyConnectivityReviewError",
    "attest_connectivity_batch",
    "execute_connectivity_batch",
    "inspect_connectivity_batch",
    "preview_connectivity_proposal_batch",
    "propose_connectivity_batch",
    "reconcile_connectivity_batch",
]
