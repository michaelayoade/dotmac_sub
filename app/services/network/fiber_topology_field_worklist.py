"""Exhaustive read-only worklist for staged fiber field verification.

The worklist orders evidence-gathering needs across every latest staged fiber
source identity. It consumes the immutable field-observation projection and
never creates jobs, observations, topology decisions, change requests, assets,
connectivity, or cutover eligibility.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.services.network.fiber_topology_field_observations import (
    PROJECTION_STATES,
    SOURCE_ASSET_TYPES,
    project_field_verification_evidence,
)

PRIORITY_STATES = (
    "p0_evidence_drift",
    "p1_conflicting_observations",
    "p2_current_conflict",
    "p3_superseded_source",
    "p4_unobserved",
    "p5_inconclusive",
    "p6_current_agreement",
)

_PRIORITY_BY_STATE: dict[str, tuple[int, str, str]] = {
    "evidence_drift": (
        0,
        "p0_evidence_drift",
        "Inspect the immutable observation, source provenance, and attachment evidence.",
    ),
    "conflicting_observations": (
        1,
        "p1_conflicting_observations",
        "Gather an independent exact-source observation for each conflicting scope.",
    ),
    "current_conflict": (
        2,
        "p2_current_conflict",
        "Verify the reported conflict against the exact current source fact.",
    ),
    "superseded_only": (
        3,
        "p3_superseded_source",
        "Re-observe the changed latest source content; prior job evidence remains historical.",
    ),
    "unobserved": (
        4,
        "p4_unobserved",
        "Collect the first exact-source field observation through a native work order.",
    ),
    "current_inconclusive": (
        5,
        "p5_inconclusive",
        "Collect follow-up evidence for the inconclusive or inaccessible scope.",
    ),
    "current_agreement": (
        6,
        "p6_current_agreement",
        "No evidence follow-up is currently indicated; retain the observation as a fact.",
    ),
}


class FiberTopologyFieldWorklistError(ValueError):
    """Raised when a consistent exhaustive worklist snapshot cannot be read."""


@dataclass(frozen=True)
class FiberTopologyFieldWorklistReport:
    report_sha256: str
    staged_feature_count: int
    source_batch_count: int
    needs_follow_up_count: int
    current_agreement_count: int
    rows_with_current_work_orders: int
    rows_with_superseded_work_orders: int
    state_counts: dict[str, int]
    priority_counts: dict[str, int]
    asset_type_counts: dict[str, int]
    source_system_counts: dict[str, int]
    source_profile_counts: dict[str, int]
    rows: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_type_counts": self.asset_type_counts,
            "current_agreement_count": self.current_agreement_count,
            "needs_follow_up_count": self.needs_follow_up_count,
            "priority_counts": self.priority_counts,
            "report_sha256": self.report_sha256,
            "rows": list(self.rows),
            "rows_with_current_work_orders": self.rows_with_current_work_orders,
            "rows_with_superseded_work_orders": (self.rows_with_superseded_work_orders),
            "schema_version": 1,
            "source_batch_count": self.source_batch_count,
            "source_profile_counts": self.source_profile_counts,
            "source_system_counts": self.source_system_counts,
            "staged_feature_count": self.staged_feature_count,
            "state_counts": self.state_counts,
        }


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def ensure_field_worklist_repeatable_snapshot(db: Session) -> None:
    """Require one consistent PostgreSQL snapshot for the complete cohort."""

    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not db.in_transaction():
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        return
    isolation_level = db.connection().get_isolation_level().upper()
    if isolation_level not in {"REPEATABLE READ", "SERIALIZABLE"}:
        raise FiberTopologyFieldWorklistError(
            "field-verification worklist requires a fresh REPEATABLE READ transaction"
        )


def _source_key(
    feature: FiberTopologyStagedFeature,
) -> tuple[str, str, str]:
    identity = feature.external_id or f"feature:{feature.id}"
    return feature.batch.source_system, feature.asset_type, identity


def _latest_staged_features(db: Session) -> list[FiberTopologyStagedFeature]:
    rows = list(
        db.scalars(
            select(FiberTopologyStagedFeature)
            .join(FiberTopologyStagedFeature.batch)
            .options(joinedload(FiberTopologyStagedFeature.batch))
            .where(FiberTopologyStagedFeature.asset_type.in_(SOURCE_ASSET_TYPES))
            .order_by(
                FiberTopologySourceBatch.created_at.desc(),
                FiberTopologyStagedFeature.created_at.desc(),
                FiberTopologyStagedFeature.id.desc(),
            )
        )
        .unique()
        .all()
    )
    latest: list[FiberTopologyStagedFeature] = []
    seen: set[tuple[str, str, str]] = set()
    for feature in rows:
        key = _source_key(feature)
        if key in seen:
            continue
        seen.add(key)
        latest.append(feature)
    return sorted(latest, key=_source_key)


def _work_orders(
    observations: object,
) -> list[dict[str, str]]:
    if not isinstance(observations, list):
        return []
    by_id: dict[str, dict[str, str]] = {}
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        work_order_id = str(observation.get("work_order_id") or "").strip()
        public_id = str(observation.get("work_order_public_id") or "").strip()
        if work_order_id and public_id:
            by_id[work_order_id] = {
                "work_order_id": work_order_id,
                "work_order_public_id": public_id,
            }
    return [by_id[key] for key in sorted(by_id)]


def _count(values: list[str], keys: tuple[str, ...] | None = None) -> dict[str, int]:
    counts = Counter(values)
    if keys is not None:
        return {key: counts[key] for key in keys}
    return dict(sorted(counts.items()))


def reconcile_fiber_field_worklist(
    db: Session,
) -> FiberTopologyFieldWorklistReport:
    """Build the complete latest-source worklist without writing state."""

    ensure_field_worklist_repeatable_snapshot(db)
    features = _latest_staged_features(db)
    evidence_by_feature = project_field_verification_evidence(db, features)
    rows: list[dict[str, object]] = []
    for feature in features:
        evidence = evidence_by_feature[str(feature.id)]
        state = str(evidence["state"])
        if state not in _PRIORITY_BY_STATE:
            raise FiberTopologyFieldWorklistError(
                f"unsupported field-verification projection state: {state}"
            )
        priority_rank, priority, next_evidence_step = _PRIORITY_BY_STATE[state]
        current_work_orders = _work_orders(evidence.get("current_observations"))
        superseded_work_orders = _work_orders(evidence.get("superseded_observations"))
        row_payload: dict[str, object] = {
            "asset_type": feature.asset_type,
            "batch_created_at": _timestamp(feature.batch.created_at),
            "blocker_codes": list(feature.blocker_codes or []),
            "content_sha256": feature.content_sha256,
            "current_work_orders": current_work_orders,
            "display_name": feature.display_name,
            "external_id": feature.external_id,
            "feature_created_at": _timestamp(feature.created_at),
            "field_verification": evidence,
            "geometry_sha256": feature.geometry_sha256,
            "geometry_type": feature.geometry_type,
            "match_status": feature.match_status,
            "needs_follow_up": state != "current_agreement",
            "next_evidence_step": next_evidence_step,
            "priority": priority,
            "priority_rank": priority_rank,
            "source_batch_id": str(feature.batch_id),
            "source_profile": feature.batch.profile,
            "source_system": feature.batch.source_system,
            "staged_feature_id": str(feature.id),
            "superseded_work_orders": superseded_work_orders,
            "verification_state": state,
        }
        row_payload["row_sha256"] = _digest(row_payload)
        rows.append(row_payload)

    rows.sort(
        key=lambda row: (
            cast(int, row["priority_rank"]),
            str(row["source_system"]),
            str(row["source_profile"]),
            str(row["asset_type"]),
            str(row["external_id"] or row["staged_feature_id"]),
        )
    )
    state_values = [str(row["verification_state"]) for row in rows]
    priority_values = [str(row["priority"]) for row in rows]
    source_batch_ids = {str(row["source_batch_id"]) for row in rows}
    asset_type_counts = _count([str(row["asset_type"]) for row in rows])
    current_agreement_count = state_values.count("current_agreement")
    needs_follow_up_count = sum(bool(row["needs_follow_up"]) for row in rows)
    priority_counts = _count(priority_values, PRIORITY_STATES)
    rows_with_current_work_orders = sum(
        bool(row["current_work_orders"]) for row in rows
    )
    rows_with_superseded_work_orders = sum(
        bool(row["superseded_work_orders"]) for row in rows
    )
    source_profile_counts = _count(
        [f"{row['source_system']}/{row['source_profile']}" for row in rows]
    )
    source_system_counts = _count([str(row["source_system"]) for row in rows])
    state_counts = _count(state_values, PROJECTION_STATES)
    report_payload: dict[str, object] = {
        "asset_type_counts": asset_type_counts,
        "current_agreement_count": current_agreement_count,
        "needs_follow_up_count": needs_follow_up_count,
        "priority_counts": priority_counts,
        "rows": rows,
        "rows_with_current_work_orders": rows_with_current_work_orders,
        "rows_with_superseded_work_orders": rows_with_superseded_work_orders,
        "schema_version": 1,
        "source_batch_count": len(source_batch_ids),
        "source_profile_counts": source_profile_counts,
        "source_system_counts": source_system_counts,
        "staged_feature_count": len(rows),
        "state_counts": state_counts,
    }
    return FiberTopologyFieldWorklistReport(
        report_sha256=_digest(report_payload),
        staged_feature_count=len(rows),
        source_batch_count=len(source_batch_ids),
        needs_follow_up_count=needs_follow_up_count,
        current_agreement_count=current_agreement_count,
        rows_with_current_work_orders=rows_with_current_work_orders,
        rows_with_superseded_work_orders=rows_with_superseded_work_orders,
        state_counts=state_counts,
        priority_counts=priority_counts,
        asset_type_counts=asset_type_counts,
        source_system_counts=source_system_counts,
        source_profile_counts=source_profile_counts,
        rows=tuple(rows),
    )


__all__ = [
    "PRIORITY_STATES",
    "FiberTopologyFieldWorklistError",
    "FiberTopologyFieldWorklistReport",
    "ensure_field_worklist_repeatable_snapshot",
    "reconcile_fiber_field_worklist",
]
