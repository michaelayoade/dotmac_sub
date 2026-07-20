"""Exact read-only fiber evidence map for one native Sub work order.

The projection joins immutable field-observation identities to the complete
field-verification exact-GeoJSON overlay. It returns only features represented by that
job's observations, strips every other work order's evidence, and fails closed
unless every immutable job observation maps to exactly one overlay feature.
It never assigns work, records observations, repairs geometry, infers topology,
or decides customer impact or cutover eligibility.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.work_order import WorkOrder
from app.schemas.status_presentation import StatusIcon, StatusPresentation, StatusTone
from app.services.network.fiber_topology_field_map import (
    GEOMETRY_PRESENTATION_STATES,
    ensure_fiber_field_map_repeatable_snapshot,
    project_fiber_field_verification_map,
)
from app.services.network.fiber_topology_field_observations import (
    list_fiber_field_observations,
    observation_to_dict,
)

WORK_ORDER_EVIDENCE_CONTEXTS = (
    "current_source",
    "superseded_source",
    "current_and_superseded_source",
)

_WORK_ORDER_EVIDENCE_PRESENTATIONS = {
    "current_source": StatusPresentation(
        value="current_source",
        label="Current source",
        tone=StatusTone.positive,
        icon=StatusIcon.check,
    ),
    "superseded_source": StatusPresentation(
        value="superseded_source",
        label="Superseded source",
        tone=StatusTone.warning,
        icon=StatusIcon.clock,
    ),
    "current_and_superseded_source": StatusPresentation(
        value="current_and_superseded_source",
        label="Current and superseded source",
        tone=StatusTone.info,
        icon=StatusIcon.info,
    ),
}

_GEOMETRY_PRESENTATIONS = {
    "exact_geojson": StatusPresentation(
        value="exact_geojson",
        label="Exact source geometry",
        tone=StatusTone.positive,
        icon=StatusIcon.check,
    ),
    "source_geometry_unrenderable": StatusPresentation(
        value="source_geometry_unrenderable",
        label="Source geometry cannot be rendered unchanged",
        tone=StatusTone.warning,
        icon=StatusIcon.alert,
    ),
}


class FiberTopologyWorkOrderEvidenceMapError(ValueError):
    """Raised when exact work-order evidence cannot be projected safely."""


@dataclass(frozen=True)
class FiberTopologyWorkOrderEvidenceMapReport:
    report_sha256: str
    source_overlay_sha256: str
    worklist_report_sha256: str
    observation_evidence_sha256: str
    work_order_id: str
    work_order_public_id: str
    observation_count: int
    current_source_observation_count: int
    superseded_source_observation_count: int
    feature_count: int
    evidence_context_counts: dict[str, int]
    geometry_presentation_counts: dict[str, int]
    feature_collection: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "current_source_observation_count": self.current_source_observation_count,
            "evidence_context_counts": self.evidence_context_counts,
            "feature_collection": self.feature_collection,
            "feature_count": self.feature_count,
            "geometry_presentation_counts": self.geometry_presentation_counts,
            "observation_count": self.observation_count,
            "observation_evidence_sha256": self.observation_evidence_sha256,
            "report_sha256": self.report_sha256,
            "schema_version": 1,
            "source_overlay_sha256": self.source_overlay_sha256,
            "superseded_source_observation_count": (
                self.superseded_source_observation_count
            ),
            "work_order_id": self.work_order_id,
            "work_order_public_id": self.work_order_public_id,
            "worklist_report_sha256": self.worklist_report_sha256,
        }


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _uuid(value: object, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise FiberTopologyWorkOrderEvidenceMapError(
            f"{field} must be a valid UUID"
        ) from exc


def _dict_list(value: object, field: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise FiberTopologyWorkOrderEvidenceMapError(
            f"Field-verification overlay has invalid {field} evidence"
        )
    if not all(isinstance(row, dict) for row in value):
        raise FiberTopologyWorkOrderEvidenceMapError(
            f"Field-verification overlay has invalid {field} evidence"
        )
    return [row for row in value if isinstance(row, dict)]


def _observation_ids(
    properties: dict[str, object],
    *,
    work_order_id: str,
) -> tuple[list[str], list[str]]:
    evidence = properties.get("field_verification")
    if not isinstance(evidence, dict):
        raise FiberTopologyWorkOrderEvidenceMapError(
            "Field-verification overlay feature has no exact evidence"
        )
    current = _dict_list(evidence.get("current_observations"), "current observation")
    superseded = _dict_list(
        evidence.get("superseded_observations"), "superseded observation"
    )

    def matching_ids(rows: list[dict[str, object]]) -> list[str]:
        result: list[str] = []
        for row in rows:
            if str(row.get("work_order_id") or "") != work_order_id:
                continue
            observation_id = str(row.get("observation_id") or "").strip()
            if not observation_id:
                raise FiberTopologyWorkOrderEvidenceMapError(
                    "Work-order evidence has no observation identity"
                )
            result.append(observation_id)
        return sorted(result)

    return matching_ids(current), matching_ids(superseded)


def _source_identity_matches(
    observation: dict[str, object], properties: dict[str, object]
) -> bool:
    return all(
        observation.get(observation_key) == properties.get(property_key)
        for observation_key, property_key in (
            ("source_system", "source_system"),
            ("source_asset_type", "asset_type"),
            ("source_external_id", "external_id"),
        )
    )


def _selected_observations(
    observation_ids: list[str],
    observations_by_id: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    return [copy.deepcopy(observations_by_id[value]) for value in observation_ids]


def ensure_work_order_evidence_map_repeatable_snapshot(db: Session) -> None:
    """Open the field-verification map owner's snapshot before permission reads."""

    ensure_fiber_field_map_repeatable_snapshot(db)


def project_fiber_work_order_evidence_map(
    db: Session,
    *,
    work_order_id: object,
    expected_work_order_public_id: str | None = None,
) -> FiberTopologyWorkOrderEvidenceMapReport:
    """Project one exact, exhaustive, read-only work-order evidence overlay."""

    ensure_work_order_evidence_map_repeatable_snapshot(db)
    work_order_uuid = _uuid(work_order_id, "work_order_id")
    work_order = db.get(WorkOrder, work_order_uuid)
    if work_order is None or not work_order.is_active:
        raise FiberTopologyWorkOrderEvidenceMapError("active work order not found")
    if (
        expected_work_order_public_id is not None
        and work_order.public_id != expected_work_order_public_id
    ):
        raise FiberTopologyWorkOrderEvidenceMapError(
            "work order public identity changed while projecting evidence"
        )

    source_map = project_fiber_field_verification_map(db)
    observation_rows = list_fiber_field_observations(
        db,
        work_order_id=work_order_uuid,
    )
    observation_payloads = [observation_to_dict(row) for row in observation_rows]
    observations_by_id = {
        str(payload["observation_id"]): payload for payload in observation_payloads
    }
    if len(observations_by_id) != len(observation_payloads):
        raise FiberTopologyWorkOrderEvidenceMapError(
            "work order has duplicate immutable observation identities"
        )
    for payload in observation_payloads:
        if (
            payload.get("work_order_id") != str(work_order.id)
            or payload.get("work_order_public_id") != work_order.public_id
        ):
            raise FiberTopologyWorkOrderEvidenceMapError(
                "immutable observation does not match the current work-order identity"
            )

    source_features = source_map.feature_collection.get("features")
    if not isinstance(source_features, list):
        raise FiberTopologyWorkOrderEvidenceMapError(
            "Field-verification overlay has no exact feature cohort"
        )

    matches_by_observation: dict[str, list[str]] = defaultdict(list)
    selected_features: list[dict[str, object]] = []
    context_values: list[str] = []
    geometry_states: list[str] = []
    current_observation_ids: set[str] = set()
    superseded_observation_ids: set[str] = set()
    work_order_id_text = str(work_order.id)

    for source_feature in source_features:
        if not isinstance(source_feature, dict):
            raise FiberTopologyWorkOrderEvidenceMapError(
                "Field-verification overlay contains a non-object feature"
            )
        properties = source_feature.get("properties")
        if not isinstance(properties, dict):
            raise FiberTopologyWorkOrderEvidenceMapError(
                "Field-verification overlay feature has no exact properties"
            )
        current_ids, superseded_ids = _observation_ids(
            properties,
            work_order_id=work_order_id_text,
        )
        if set(current_ids) & set(superseded_ids):
            raise FiberTopologyWorkOrderEvidenceMapError(
                "one observation cannot be both current and superseded"
            )
        if not current_ids and not superseded_ids:
            continue

        feature_id = str(source_feature.get("id") or "").strip()
        if not feature_id:
            raise FiberTopologyWorkOrderEvidenceMapError(
                "Work-order evidence feature has no staged identity"
            )
        for observation_id in current_ids + superseded_ids:
            observation = observations_by_id.get(observation_id)
            if observation is None:
                raise FiberTopologyWorkOrderEvidenceMapError(
                    "Field-verification overlay references observation evidence outside the work order"
                )
            if not _source_identity_matches(observation, properties):
                raise FiberTopologyWorkOrderEvidenceMapError(
                    "work-order observation source identity does not match the overlay"
                )
            is_same_content = observation.get(
                "feature_content_sha256"
            ) == properties.get("content_sha256")
            if observation_id in current_ids and not is_same_content:
                raise FiberTopologyWorkOrderEvidenceMapError(
                    "current work-order observation content does not match the overlay"
                )
            if observation_id in superseded_ids and is_same_content:
                raise FiberTopologyWorkOrderEvidenceMapError(
                    "superseded work-order observation unexpectedly matches current content"
                )
            matches_by_observation[observation_id].append(feature_id)

        current_observation_ids.update(current_ids)
        superseded_observation_ids.update(superseded_ids)
        if current_ids and superseded_ids:
            context = "current_and_superseded_source"
        elif current_ids:
            context = "current_source"
        else:
            context = "superseded_source"

        selected = copy.deepcopy(source_feature)
        selected_properties = selected.get("properties")
        if not isinstance(selected_properties, dict):
            raise FiberTopologyWorkOrderEvidenceMapError(
                "Field-verification overlay feature properties changed while projecting"
            )
        selected_properties.pop("field_verification", None)
        selected_properties.pop("current_work_orders", None)
        selected_properties.pop("superseded_work_orders", None)
        selected_properties["work_order_evidence"] = {
            "context": context,
            "context_presentation": _WORK_ORDER_EVIDENCE_PRESENTATIONS[
                context
            ].model_dump(mode="json"),
            "current_observation_count": len(current_ids),
            "current_observations": _selected_observations(
                current_ids, observations_by_id
            ),
            "superseded_observation_count": len(superseded_ids),
            "superseded_observations": _selected_observations(
                superseded_ids, observations_by_id
            ),
            "work_order_id": work_order_id_text,
            "work_order_public_id": work_order.public_id,
        }
        selected_properties["work_order_evidence_sha256"] = _digest(
            selected_properties["work_order_evidence"]
        )
        selected_features.append(selected)
        context_values.append(context)
        geometry_state = str(
            selected_properties.get("geometry_presentation_state") or ""
        )
        if geometry_state not in GEOMETRY_PRESENTATION_STATES:
            raise FiberTopologyWorkOrderEvidenceMapError(
                "Work-order evidence feature has an invalid geometry presentation state"
            )
        selected_properties["geometry_presentation"] = _GEOMETRY_PRESENTATIONS[
            geometry_state
        ].model_dump(mode="json")
        selected_properties["work_order_map_feature_sha256"] = _digest(selected)
        geometry_states.append(geometry_state)

    missing = sorted(set(observations_by_id) - set(matches_by_observation))
    ambiguous = sorted(
        observation_id
        for observation_id, feature_ids in matches_by_observation.items()
        if len(feature_ids) != 1
    )
    if missing or ambiguous:
        details: list[str] = []
        if missing:
            details.append("unmapped=" + ",".join(missing))
        if ambiguous:
            details.append("ambiguous=" + ",".join(ambiguous))
        raise FiberTopologyWorkOrderEvidenceMapError(
            "every immutable work-order observation must map to exactly one "
            "Field-verification feature: " + "; ".join(details)
        )

    feature_collection: dict[str, object] = {
        "features": selected_features,
        "type": "FeatureCollection",
    }
    context_counter = Counter(context_values)
    geometry_counter = Counter(geometry_states)
    evidence_context_counts = {
        value: context_counter[value] for value in WORK_ORDER_EVIDENCE_CONTEXTS
    }
    geometry_presentation_counts = {
        value: geometry_counter[value] for value in GEOMETRY_PRESENTATION_STATES
    }
    observation_evidence_sha256 = _digest(
        sorted(observation_payloads, key=lambda row: str(row["observation_id"]))
    )
    report_payload: dict[str, object] = {
        "current_source_observation_count": len(current_observation_ids),
        "evidence_context_counts": evidence_context_counts,
        "feature_collection": feature_collection,
        "feature_count": len(selected_features),
        "geometry_presentation_counts": geometry_presentation_counts,
        "observation_count": len(observation_payloads),
        "observation_evidence_sha256": observation_evidence_sha256,
        "schema_version": 1,
        "source_overlay_sha256": source_map.overlay_sha256,
        "superseded_source_observation_count": len(superseded_observation_ids),
        "work_order_id": work_order_id_text,
        "work_order_public_id": work_order.public_id,
        "worklist_report_sha256": source_map.worklist_report_sha256,
    }
    return FiberTopologyWorkOrderEvidenceMapReport(
        report_sha256=_digest(report_payload),
        source_overlay_sha256=source_map.overlay_sha256,
        worklist_report_sha256=source_map.worklist_report_sha256,
        observation_evidence_sha256=observation_evidence_sha256,
        work_order_id=work_order_id_text,
        work_order_public_id=work_order.public_id,
        observation_count=len(observation_payloads),
        current_source_observation_count=len(current_observation_ids),
        superseded_source_observation_count=len(superseded_observation_ids),
        feature_count=len(selected_features),
        evidence_context_counts=evidence_context_counts,
        geometry_presentation_counts=geometry_presentation_counts,
        feature_collection=feature_collection,
    )


__all__ = [
    "WORK_ORDER_EVIDENCE_CONTEXTS",
    "FiberTopologyWorkOrderEvidenceMapError",
    "FiberTopologyWorkOrderEvidenceMapReport",
    "ensure_work_order_evidence_map_repeatable_snapshot",
    "project_fiber_work_order_evidence_map",
]
