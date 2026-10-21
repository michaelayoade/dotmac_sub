"""Exact source scope stored on native fiber field-verification jobs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from uuid import UUID

from app.models.fiber_topology_staging import FiberTopologyStagedFeature
from app.models.work_order import WorkOrder
from app.services.network.fiber_field_verification_job_plan_contracts import (
    FiberFeatureEvidence,
    FiberJobPlanScope,
    FiberVerificationPlanProvenance,
    FiberWorkOrderReference,
)

PLAN_METADATA_KEY = "fiber_field_verification_plan"
_SHA256_HEX = frozenset("0123456789abcdef")


class FiberFieldVerificationJobScopeError(ValueError):
    """Raised when a planned job's immutable source scope is invalid."""


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _sha256(value: object, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(char not in _SHA256_HEX for char in normalized):
        raise FiberFieldVerificationJobScopeError(
            f"fiber field-verification job {field} is invalid"
        )
    return normalized


def serialize_feature_evidence(feature: FiberFeatureEvidence) -> dict[str, object]:
    """Serialize the historical source-scope representation without changing hashes."""

    def references(values: tuple[FiberWorkOrderReference, ...]) -> list[dict[str, str]]:
        return [
            {
                "work_order_id": str(value.work_order_id),
                "work_order_public_id": value.work_order_public_id,
            }
            for value in values
        ]

    return {
        "asset_type": feature.asset_type,
        "content_sha256": feature.content_sha256,
        "current_work_orders": references(feature.current_work_orders),
        "display_name": feature.display_name,
        "external_id": feature.external_id,
        "geometry_sha256": feature.geometry_sha256,
        "geometry_type": feature.geometry_type,
        "needs_follow_up": feature.needs_follow_up,
        "priority": feature.priority,
        "row_sha256": feature.row_sha256,
        "source_batch_id": str(feature.source_batch_id),
        "source_profile": feature.source_profile,
        "source_system": feature.source_system,
        "staged_feature_id": str(feature.staged_feature_id),
        "superseded_work_orders": references(feature.superseded_work_orders),
        "verification_state": feature.verification_state,
    }


def _scope_payload(
    *, selected_features: list[dict[str, object]], worklist_report_sha256: str
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "selected_feature_count": len(selected_features),
        "selected_features": selected_features,
        "worklist_report_sha256": worklist_report_sha256,
    }


def build_planned_scope_metadata(
    *, selected_features: tuple[FiberFeatureEvidence, ...], worklist_report_sha256: str
) -> FiberJobPlanScope:
    payload = _scope_payload(
        selected_features=[
            serialize_feature_evidence(feature) for feature in selected_features
        ],
        worklist_report_sha256=worklist_report_sha256,
    )
    return FiberJobPlanScope(
        selected_features, worklist_report_sha256, _digest(payload)
    )


def planned_scope_payload(scope: FiberJobPlanScope) -> dict[str, object]:
    return {
        **_scope_payload(
            selected_features=[
                serialize_feature_evidence(feature)
                for feature in scope.selected_features
            ],
            worklist_report_sha256=scope.worklist_report_sha256,
        ),
        "scope_sha256": scope.scope_sha256,
    }


def serialize_fiber_plan_provenance(
    value: FiberVerificationPlanProvenance,
) -> dict[str, object]:
    return {"plan_sha256": value.plan_sha256, **planned_scope_payload(value.scope)}


def planned_feature_scope(
    work_order: WorkOrder,
) -> tuple[FiberFeatureEvidence, ...] | None:
    """Return an exact feature-id scope, or ``None`` for a legacy unplanned job."""

    metadata = work_order.metadata_ if isinstance(work_order.metadata_, dict) else {}
    raw_plan = metadata.get(PLAN_METADATA_KEY)
    if raw_plan is None:
        return None
    if not isinstance(raw_plan, dict) or raw_plan.get("schema_version") != 1:
        raise FiberFieldVerificationJobScopeError(
            "fiber field-verification job plan metadata is invalid"
        )
    raw_features = raw_plan.get("selected_features")
    if not isinstance(raw_features, list) or not raw_features:
        raise FiberFieldVerificationJobScopeError(
            "fiber field-verification job plan has no selected source features"
        )
    scope: dict[str, Mapping[str, object]] = {}
    _sha256(raw_plan.get("plan_sha256"), "plan digest")
    scope_sha256 = _sha256(raw_plan.get("scope_sha256"), "source-scope digest")
    worklist_report_sha256 = _sha256(
        raw_plan.get("worklist_report_sha256"), "worklist report digest"
    )
    for raw_feature in raw_features:
        if not isinstance(raw_feature, dict):
            raise FiberFieldVerificationJobScopeError(
                "fiber field-verification job source scope is invalid"
            )
        feature_id = str(raw_feature.get("staged_feature_id") or "").strip()
        try:
            _sha256(raw_feature.get("content_sha256"), "source content digest")
            _sha256(raw_feature.get("row_sha256"), "worklist row digest")
            if raw_feature.get("geometry_sha256") is not None:
                _sha256(raw_feature.get("geometry_sha256"), "source geometry digest")
        except FiberFieldVerificationJobScopeError as exc:
            raise FiberFieldVerificationJobScopeError(
                "fiber field-verification job source scope is invalid"
            ) from exc
        if not feature_id or feature_id in scope:
            raise FiberFieldVerificationJobScopeError(
                "fiber field-verification job source scope is invalid"
            )
        scope[feature_id] = raw_feature
    if raw_plan.get("selected_feature_count") != len(scope):
        raise FiberFieldVerificationJobScopeError(
            "fiber field-verification job source count does not match its scope"
        )
    expected_scope = _scope_payload(
        selected_features=raw_features,
        worklist_report_sha256=worklist_report_sha256,
    )
    if _digest(expected_scope) != scope_sha256:
        raise FiberFieldVerificationJobScopeError(
            "fiber field-verification job source scope digest does not match"
        )
    return tuple(_parse_feature(value) for value in scope.values())


def _parse_feature(row: Mapping[str, object]) -> FiberFeatureEvidence:
    def optional_text(key: str) -> str | None:
        value = row.get(key)
        return str(value) if value is not None else None

    def references(key: str) -> tuple[FiberWorkOrderReference, ...]:
        values = row.get(key)
        if not isinstance(values, list):
            raise FiberFieldVerificationJobScopeError(
                "invalid planned work-order references"
            )
        result = []
        for value in values:
            if not isinstance(value, dict):
                raise FiberFieldVerificationJobScopeError(
                    "invalid planned work-order reference"
                )
            result.append(
                FiberWorkOrderReference(
                    UUID(str(value["work_order_id"])),
                    str(value["work_order_public_id"]),
                )
            )
        return tuple(result)

    try:
        return FiberFeatureEvidence(
            asset_type=str(row["asset_type"]),
            content_sha256=str(row["content_sha256"]),
            current_work_orders=references("current_work_orders"),
            display_name=optional_text("display_name"),
            external_id=optional_text("external_id"),
            geometry_sha256=optional_text("geometry_sha256"),
            geometry_type=optional_text("geometry_type"),
            needs_follow_up=bool(row["needs_follow_up"]),
            priority=str(row["priority"]),
            row_sha256=str(row["row_sha256"]),
            source_batch_id=UUID(str(row["source_batch_id"])),
            source_profile=str(row["source_profile"]),
            source_system=str(row["source_system"]),
            staged_feature_id=UUID(str(row["staged_feature_id"])),
            superseded_work_orders=references("superseded_work_orders"),
            verification_state=str(row["verification_state"]),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise FiberFieldVerificationJobScopeError(
            "invalid planned source evidence"
        ) from exc


def assert_feature_in_planned_scope(
    work_order: WorkOrder,
    feature: FiberTopologyStagedFeature,
) -> None:
    """Fail closed when an explicitly planned job observes outside its scope."""

    scope = planned_feature_scope(work_order)
    if scope is None:
        return
    planned = next(
        (value for value in scope if value.staged_feature_id == feature.id), None
    )
    if planned is None:
        raise FiberFieldVerificationJobScopeError(
            "staged feature is outside this work order's verification plan"
        )
    if planned.content_sha256 != feature.content_sha256:
        raise FiberFieldVerificationJobScopeError(
            "planned staged feature content has changed"
        )


__all__ = [
    "PLAN_METADATA_KEY",
    "FiberFieldVerificationJobScopeError",
    "assert_feature_in_planned_scope",
    "build_planned_scope_metadata",
    "planned_feature_scope",
    "planned_scope_payload",
    "serialize_feature_evidence",
    "serialize_fiber_plan_provenance",
]
