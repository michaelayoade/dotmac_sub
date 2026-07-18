"""Exact source scope stored on native fiber field-verification jobs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.models.fiber_topology_staging import FiberTopologyStagedFeature
from app.models.work_order import WorkOrder

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


def build_planned_scope_metadata(
    *,
    selected_features: Sequence[Mapping[str, Any]],
    worklist_report_sha256: str,
) -> dict[str, Any]:
    """Build the versioned, self-digesting metadata owned by this contract."""

    scope_payload: dict[str, Any] = {
        "schema_version": 1,
        "selected_feature_count": len(selected_features),
        "selected_features": [dict(feature) for feature in selected_features],
        "worklist_report_sha256": worklist_report_sha256,
    }
    return {**scope_payload, "scope_sha256": _digest(scope_payload)}


def planned_feature_scope(
    work_order: WorkOrder,
) -> dict[str, dict[str, Any]] | None:
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
    scope: dict[str, dict[str, Any]] = {}
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
    expected_scope = build_planned_scope_metadata(
        selected_features=raw_features,
        worklist_report_sha256=worklist_report_sha256,
    )
    if expected_scope["scope_sha256"] != scope_sha256:
        raise FiberFieldVerificationJobScopeError(
            "fiber field-verification job source scope digest does not match"
        )
    return scope


def assert_feature_in_planned_scope(
    work_order: WorkOrder,
    feature: FiberTopologyStagedFeature,
) -> None:
    """Fail closed when an explicitly planned job observes outside its scope."""

    scope = planned_feature_scope(work_order)
    if scope is None:
        return
    planned = scope.get(str(feature.id))
    if planned is None:
        raise FiberFieldVerificationJobScopeError(
            "staged feature is outside this work order's verification plan"
        )
    if planned.get("content_sha256") != feature.content_sha256:
        raise FiberFieldVerificationJobScopeError(
            "planned staged feature content has changed"
        )


__all__ = [
    "PLAN_METADATA_KEY",
    "FiberFieldVerificationJobScopeError",
    "assert_feature_in_planned_scope",
    "build_planned_scope_metadata",
    "planned_feature_scope",
]
