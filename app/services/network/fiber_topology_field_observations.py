"""Immutable field observations for exact staged fiber source evidence.

Field observations are facts, not topology decisions. This owner binds each
fact to the exact staged content, work order, technician, explicit references,
and active attachment pointers. It retains disagreement and drift but never
proposes identity/connectivity, selects from geometry, approves changes, or
mutates canonical fiber assets.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.models.dispatch import TechnicianProfile
from app.models.fiber_topology_field_observation import (
    FiberTopologyFieldObservation,
)
from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.field_attachment import FieldAttachment
from app.models.gis import ServiceBuilding
from app.models.network import (
    FdhCabinet,
    FiberAccessPoint,
    FiberSpliceClosure,
    OntUnit,
    PonPort,
    SplitterPort,
)
from app.models.work_order import WorkOrder

POINT_ASSET_TYPES = frozenset(
    {
        "fdh_cabinet",
        "fiber_access_point",
        "service_building",
        "splice_closure",
        "support_structure",
    }
)
SOURCE_ASSET_TYPES = frozenset({*POINT_ASSET_TYPES, "fiber_segment"})
VERIFICATION_SCOPES = frozenset(
    {"identity", "presence", "start_endpoint", "end_endpoint", "path_endpoints"}
)
OBSERVATION_OUTCOMES = frozenset(
    {"agrees", "conflicts", "not_found", "inaccessible", "inconclusive"}
)
PROJECTION_STATES = (
    "unobserved",
    "superseded_only",
    "current_agreement",
    "current_conflict",
    "current_inconclusive",
    "conflicting_observations",
    "evidence_drift",
)
POINT_ASSET_MODELS: dict[str, type] = {
    "fdh_cabinet": FdhCabinet,
    "fiber_access_point": FiberAccessPoint,
    "service_building": ServiceBuilding,
    "splice_closure": FiberSpliceClosure,
}
ENDPOINT_MODELS: dict[str, type] = {
    "fdh": FdhCabinet,
    "fiber_access_point": FiberAccessPoint,
    "ont": OntUnit,
    "pon_port": PonPort,
    "splice_closure": FiberSpliceClosure,
    "splitter_port": SplitterPort,
}


class FiberTopologyFieldObservationError(ValueError):
    """Raised when staged field evidence is structurally invalid or stale."""


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _coerce_uuid(value: object, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise FiberTopologyFieldObservationError(f"{field} must be a UUID") from exc


def _required_text(value: object, field: str, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise FiberTopologyFieldObservationError(f"{field} is required")
    if len(normalized) > limit:
        raise FiberTopologyFieldObservationError(
            f"{field} must be at most {limit} characters"
        )
    return normalized


def _optional_text(value: object, field: str, *, limit: int) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if len(normalized) > limit:
        raise FiberTopologyFieldObservationError(
            f"{field} must be at most {limit} characters"
        )
    return normalized


def _expected_sha256(value: object) -> str:
    digest = _required_text(value, "expected_feature_content_sha256", limit=64)
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise FiberTopologyFieldObservationError(
            "expected_feature_content_sha256 must be a lowercase SHA-256 digest"
        )
    return digest


def _normalized_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FiberTopologyFieldObservationError("observed_at must be timezone-aware")
    normalized = value.astimezone(UTC)
    if normalized > datetime.now(UTC) + timedelta(minutes=5):
        raise FiberTopologyFieldObservationError(
            "observed_at cannot be more than five minutes in the future"
        )
    return normalized


def _timestamp(value: datetime) -> str:
    """Render DB timestamps consistently even when SQLite drops timezone data."""

    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _optional_float(
    value: object,
    field: str,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    if value is None:
        return None
    try:
        normalized = float(str(value))
    except (TypeError, ValueError) as exc:
        raise FiberTopologyFieldObservationError(f"{field} must be numeric") from exc
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        raise FiberTopologyFieldObservationError(
            f"{field} must be between {minimum:g} and {maximum:g}"
        )
    return normalized


def _measurement_payload(value: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = dict(value or {})
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise FiberTopologyFieldObservationError(
            "measurement_payload must contain JSON-compatible finite values"
        ) from exc
    if len(encoded.encode()) > 20_000:
        raise FiberTopologyFieldObservationError(
            "measurement_payload must be at most 20000 encoded bytes"
        )
    return json.loads(encoded)


def _load_feature(db: Session, staged_feature_id: object) -> FiberTopologyStagedFeature:
    feature = db.scalar(
        select(FiberTopologyStagedFeature)
        .options(joinedload(FiberTopologyStagedFeature.batch))
        .where(
            FiberTopologyStagedFeature.id
            == _coerce_uuid(staged_feature_id, "staged_feature_id")
        )
    )
    if feature is None:
        raise FiberTopologyFieldObservationError("staged feature not found")
    if feature.asset_type not in SOURCE_ASSET_TYPES:
        raise FiberTopologyFieldObservationError(
            "field verification supports staged point assets and fiber segments only"
        )
    return feature


def _assert_current_content(
    db: Session,
    feature: FiberTopologyStagedFeature,
    expected_feature_content_sha256: object,
) -> None:
    expected = _expected_sha256(expected_feature_content_sha256)
    if feature.content_sha256 != expected:
        raise FiberTopologyFieldObservationError(
            "staged feature content does not match the expected SHA-256"
        )
    if feature.external_id is None:
        return
    latest = db.scalar(
        select(FiberTopologyStagedFeature)
        .join(FiberTopologyStagedFeature.batch)
        .where(
            FiberTopologySourceBatch.source_system == feature.batch.source_system,
            FiberTopologyStagedFeature.asset_type == feature.asset_type,
            FiberTopologyStagedFeature.external_id == feature.external_id,
        )
        .order_by(
            FiberTopologySourceBatch.created_at.desc(),
            FiberTopologyStagedFeature.created_at.desc(),
            FiberTopologyStagedFeature.id.desc(),
        )
    )
    if latest and latest.content_sha256 != feature.content_sha256:
        raise FiberTopologyFieldObservationError(
            "a newer staged source version changed this feature; observe the latest content"
        )


def _load_actor_and_work_order(
    db: Session,
    *,
    work_order_id: object,
    recorded_by_technician_id: object,
    recorded_by_person_id: object,
    recorded_by_system_user_id: object | None,
) -> tuple[WorkOrder, TechnicianProfile, uuid.UUID, uuid.UUID | None]:
    work_order_uuid = _coerce_uuid(work_order_id, "work_order_id")
    work_order = db.get(WorkOrder, work_order_uuid)
    if work_order is None or not work_order.is_active:
        raise FiberTopologyFieldObservationError("active work order not found")
    technician_uuid = _coerce_uuid(
        recorded_by_technician_id, "recorded_by_technician_id"
    )
    technician = db.get(TechnicianProfile, technician_uuid)
    if technician is None or not technician.is_active:
        raise FiberTopologyFieldObservationError("active technician not found")
    person_uuid = _coerce_uuid(recorded_by_person_id, "recorded_by_person_id")
    if technician.person_id != person_uuid:
        raise FiberTopologyFieldObservationError(
            "technician person identity does not match the observation actor"
        )
    system_user_uuid = (
        _coerce_uuid(recorded_by_system_user_id, "recorded_by_system_user_id")
        if recorded_by_system_user_id is not None
        else None
    )
    if technician.system_user_id != system_user_uuid:
        raise FiberTopologyFieldObservationError(
            "technician system-user identity does not match the observation actor"
        )
    return work_order, technician, person_uuid, system_user_uuid


def _attachment_ids(
    db: Session,
    values: Sequence[object] | None,
    *,
    work_order_id: uuid.UUID,
) -> tuple[str, ...]:
    if values is None:
        return ()
    if len(values) > 20:
        raise FiberTopologyFieldObservationError(
            "attachment_ids cannot contain more than 20 entries"
        )
    ids = tuple(sorted({_coerce_uuid(value, "attachment_ids") for value in values}))
    if len(ids) != len(values):
        raise FiberTopologyFieldObservationError("attachment_ids must be unique")
    attachments = (
        list(
            db.scalars(select(FieldAttachment).where(FieldAttachment.id.in_(ids))).all()
        )
        if ids
        else []
    )
    if len(attachments) != len(ids) or any(
        not attachment.is_active or attachment.work_order_mirror_id != work_order_id
        for attachment in attachments
    ):
        raise FiberTopologyFieldObservationError(
            "every attachment must be active and belong to the same work order"
        )
    return tuple(str(value) for value in ids)


def _canonical_reference(
    db: Session,
    *,
    asset_type: object | None,
    asset_id: object | None,
    source_asset_type: str,
) -> tuple[str | None, uuid.UUID | None]:
    if asset_type is None and asset_id is None:
        return None, None
    if asset_type is None or asset_id is None:
        raise FiberTopologyFieldObservationError(
            "observed_asset_type and observed_asset_id must be provided together"
        )
    normalized_type = _required_text(
        asset_type, "observed_asset_type", limit=40
    ).lower()
    if normalized_type != source_asset_type:
        raise FiberTopologyFieldObservationError(
            "observed canonical asset type must match the staged point-asset type"
        )
    model = POINT_ASSET_MODELS.get(normalized_type)
    if model is None:
        raise FiberTopologyFieldObservationError(
            "this staged asset type has no approved canonical observation target"
        )
    normalized_id = _coerce_uuid(asset_id, "observed_asset_id")
    asset = db.get(model, normalized_id)
    if asset is None or getattr(asset, "is_active", True) is False:
        raise FiberTopologyFieldObservationError(
            "active observed canonical asset not found"
        )
    return normalized_type, normalized_id


def _endpoint_reference(
    db: Session,
    *,
    endpoint_type: object | None,
    endpoint_ref_id: object | None,
    prefix: str,
) -> tuple[str | None, uuid.UUID | None]:
    if endpoint_type is None and endpoint_ref_id is None:
        return None, None
    if endpoint_type is None or endpoint_ref_id is None:
        raise FiberTopologyFieldObservationError(
            f"{prefix}_endpoint_type and {prefix}_endpoint_ref_id must be provided together"
        )
    normalized_type = _required_text(
        endpoint_type, f"{prefix}_endpoint_type", limit=40
    ).lower()
    model = ENDPOINT_MODELS.get(normalized_type)
    if model is None:
        raise FiberTopologyFieldObservationError(
            f"{prefix}_endpoint_type is not an approved canonical endpoint"
        )
    normalized_id = _coerce_uuid(endpoint_ref_id, f"{prefix}_endpoint_ref_id")
    endpoint = db.get(model, normalized_id)
    if endpoint is None or getattr(endpoint, "is_active", True) is False:
        raise FiberTopologyFieldObservationError(
            f"active {prefix} canonical endpoint not found"
        )
    return normalized_type, normalized_id


def _validate_scope(
    *,
    source_asset_type: str,
    verification_scope: str,
    outcome: str,
    observed_external_label: str | None,
    observed_asset_id: uuid.UUID | None,
    start_endpoint_ref_id: uuid.UUID | None,
    end_endpoint_ref_id: uuid.UUID | None,
) -> None:
    is_path = source_asset_type == "fiber_segment"
    if is_path and verification_scope not in {
        "presence",
        "start_endpoint",
        "end_endpoint",
        "path_endpoints",
    }:
        raise FiberTopologyFieldObservationError(
            "staged paths support presence or explicit endpoint verification scopes"
        )
    if not is_path and verification_scope not in {"identity", "presence"}:
        raise FiberTopologyFieldObservationError(
            "staged point assets support identity or presence verification scopes"
        )
    if verification_scope == "presence":
        if observed_asset_id or start_endpoint_ref_id or end_endpoint_ref_id:
            raise FiberTopologyFieldObservationError(
                "presence observations cannot carry canonical asset or endpoint references"
            )
        return
    if verification_scope == "identity":
        if start_endpoint_ref_id or end_endpoint_ref_id:
            raise FiberTopologyFieldObservationError(
                "identity observations cannot carry path endpoint references"
            )
        if outcome in {"agrees", "conflicts"} and not (
            observed_external_label or observed_asset_id
        ):
            raise FiberTopologyFieldObservationError(
                "agreeing or conflicting identity observations require an explicit label or canonical asset"
            )
        return
    if observed_asset_id:
        raise FiberTopologyFieldObservationError(
            "path endpoint observations cannot carry a point-asset reference"
        )
    if verification_scope == "start_endpoint" and (
        start_endpoint_ref_id is None or end_endpoint_ref_id is not None
    ):
        raise FiberTopologyFieldObservationError(
            "start_endpoint scope requires only an explicit start endpoint"
        )
    if verification_scope == "end_endpoint" and (
        end_endpoint_ref_id is None or start_endpoint_ref_id is not None
    ):
        raise FiberTopologyFieldObservationError(
            "end_endpoint scope requires only an explicit end endpoint"
        )
    if verification_scope == "path_endpoints" and (
        start_endpoint_ref_id is None or end_endpoint_ref_id is None
    ):
        raise FiberTopologyFieldObservationError(
            "path_endpoints scope requires explicit start and end endpoints"
        )


def _claim_payload(
    *,
    verification_scope: str,
    outcome: str,
    observed_external_label: str | None,
    observed_asset_type: str | None,
    observed_asset_id: uuid.UUID | None,
    start_endpoint_type: str | None,
    start_endpoint_ref_id: uuid.UUID | None,
    end_endpoint_type: str | None,
    end_endpoint_ref_id: uuid.UUID | None,
) -> dict[str, object]:
    return {
        "end_endpoint_ref_id": (
            str(end_endpoint_ref_id) if end_endpoint_ref_id else None
        ),
        "end_endpoint_type": end_endpoint_type,
        "observed_asset_id": str(observed_asset_id) if observed_asset_id else None,
        "observed_asset_type": observed_asset_type,
        "observed_external_label": observed_external_label,
        "outcome": outcome,
        "schema_version": 1,
        "start_endpoint_ref_id": (
            str(start_endpoint_ref_id) if start_endpoint_ref_id else None
        ),
        "start_endpoint_type": start_endpoint_type,
        "verification_scope": verification_scope,
    }


def _observation_payload(
    *,
    feature: FiberTopologyStagedFeature,
    work_order: WorkOrder,
    technician_id: uuid.UUID,
    person_id: uuid.UUID,
    system_user_id: uuid.UUID | None,
    claim_sha256: str,
    latitude: float | None,
    longitude: float | None,
    accuracy_m: float | None,
    instrument: str | None,
    measurement_payload: dict[str, Any],
    attachment_ids: tuple[str, ...],
    notes: str | None,
    observed_at: datetime,
) -> dict[str, object]:
    return {
        "accuracy_m": accuracy_m,
        "attachment_ids": list(attachment_ids),
        "claim_sha256": claim_sha256,
        "feature_content_sha256": feature.content_sha256,
        "instrument": instrument,
        "latitude": latitude,
        "longitude": longitude,
        "measurement_payload": measurement_payload,
        "notes": notes,
        "observed_at": _timestamp(observed_at),
        "recorded_by_person_id": str(person_id),
        "recorded_by_system_user_id": (str(system_user_id) if system_user_id else None),
        "recorded_by_technician_id": str(technician_id),
        "schema_version": 1,
        "source_asset_type": feature.asset_type,
        "source_external_id": feature.external_id,
        "source_profile": feature.batch.profile,
        "source_system": feature.batch.source_system,
        "staged_feature_id": str(feature.id),
        "work_order_id": str(work_order.id),
        "work_order_public_id": work_order.public_id,
    }


def _row_claim_payload(row: FiberTopologyFieldObservation) -> dict[str, object]:
    return _claim_payload(
        verification_scope=row.verification_scope,
        outcome=row.outcome,
        observed_external_label=row.observed_external_label,
        observed_asset_type=row.observed_asset_type,
        observed_asset_id=row.observed_asset_id,
        start_endpoint_type=row.start_endpoint_type,
        start_endpoint_ref_id=row.start_endpoint_ref_id,
        end_endpoint_type=row.end_endpoint_type,
        end_endpoint_ref_id=row.end_endpoint_ref_id,
    )


def _row_observation_payload(row: FiberTopologyFieldObservation) -> dict[str, object]:
    return {
        "accuracy_m": row.accuracy_m,
        "attachment_ids": list(row.attachment_ids or []),
        "claim_sha256": row.claim_sha256,
        "feature_content_sha256": row.feature_content_sha256,
        "instrument": row.instrument,
        "latitude": row.latitude,
        "longitude": row.longitude,
        "measurement_payload": row.measurement_payload or {},
        "notes": row.notes,
        "observed_at": _timestamp(row.observed_at),
        "recorded_by_person_id": str(row.recorded_by_person_id),
        "recorded_by_system_user_id": (
            str(row.recorded_by_system_user_id)
            if row.recorded_by_system_user_id
            else None
        ),
        "recorded_by_technician_id": str(row.recorded_by_technician_id),
        "schema_version": 1,
        "source_asset_type": row.source_asset_type,
        "source_external_id": row.source_external_id,
        "source_profile": row.source_profile,
        "source_system": row.source_system,
        "staged_feature_id": str(row.staged_feature_id),
        "work_order_id": str(row.work_order_id),
        "work_order_public_id": row.work_order_public_id,
    }


def _assert_exact_replay(
    existing: FiberTopologyFieldObservation,
    observation_sha256: str,
) -> FiberTopologyFieldObservation:
    if existing.observation_sha256 != observation_sha256:
        raise FiberTopologyFieldObservationError(
            "client_ref already belongs to a different immutable field observation"
        )
    return existing


def record_fiber_field_observation(
    db: Session,
    *,
    staged_feature_id: object,
    expected_feature_content_sha256: object,
    work_order_id: object,
    recorded_by_technician_id: object,
    recorded_by_person_id: object,
    recorded_by_system_user_id: object | None,
    verification_scope: object,
    outcome: object,
    observed_at: datetime,
    client_ref: object,
    observed_external_label: object | None = None,
    observed_asset_type: object | None = None,
    observed_asset_id: object | None = None,
    start_endpoint_type: object | None = None,
    start_endpoint_ref_id: object | None = None,
    end_endpoint_type: object | None = None,
    end_endpoint_ref_id: object | None = None,
    latitude: object | None = None,
    longitude: object | None = None,
    accuracy_m: object | None = None,
    instrument: object | None = None,
    measurement_payload: Mapping[str, Any] | None = None,
    attachment_ids: Sequence[object] | None = None,
    notes: object | None = None,
) -> FiberTopologyFieldObservation:
    """Persist one exact observation fact; never interpret it as a decision."""

    feature = _load_feature(db, staged_feature_id)
    _assert_current_content(db, feature, expected_feature_content_sha256)
    work_order, technician, person_id, system_user_id = _load_actor_and_work_order(
        db,
        work_order_id=work_order_id,
        recorded_by_technician_id=recorded_by_technician_id,
        recorded_by_person_id=recorded_by_person_id,
        recorded_by_system_user_id=recorded_by_system_user_id,
    )
    normalized_scope = _required_text(
        verification_scope, "verification_scope", limit=32
    ).lower()
    if normalized_scope not in VERIFICATION_SCOPES:
        raise FiberTopologyFieldObservationError("unsupported verification_scope")
    normalized_outcome = _required_text(outcome, "outcome", limit=24).lower()
    if normalized_outcome not in OBSERVATION_OUTCOMES:
        raise FiberTopologyFieldObservationError("unsupported observation outcome")
    normalized_label = _optional_text(
        observed_external_label, "observed_external_label", limit=255
    )
    normalized_asset_type, normalized_asset_id = _canonical_reference(
        db,
        asset_type=observed_asset_type,
        asset_id=observed_asset_id,
        source_asset_type=feature.asset_type,
    )
    normalized_start_type, normalized_start_id = _endpoint_reference(
        db,
        endpoint_type=start_endpoint_type,
        endpoint_ref_id=start_endpoint_ref_id,
        prefix="start",
    )
    normalized_end_type, normalized_end_id = _endpoint_reference(
        db,
        endpoint_type=end_endpoint_type,
        endpoint_ref_id=end_endpoint_ref_id,
        prefix="end",
    )
    _validate_scope(
        source_asset_type=feature.asset_type,
        verification_scope=normalized_scope,
        outcome=normalized_outcome,
        observed_external_label=normalized_label,
        observed_asset_id=normalized_asset_id,
        start_endpoint_ref_id=normalized_start_id,
        end_endpoint_ref_id=normalized_end_id,
    )
    normalized_latitude = _optional_float(latitude, "latitude", minimum=-90, maximum=90)
    normalized_longitude = _optional_float(
        longitude, "longitude", minimum=-180, maximum=180
    )
    if (normalized_latitude is None) != (normalized_longitude is None):
        raise FiberTopologyFieldObservationError(
            "latitude and longitude must be provided together"
        )
    normalized_accuracy = _optional_float(
        accuracy_m, "accuracy_m", minimum=0, maximum=10_000
    )
    if normalized_accuracy is not None and normalized_latitude is None:
        raise FiberTopologyFieldObservationError(
            "accuracy_m requires latitude and longitude"
        )
    normalized_instrument = _optional_text(instrument, "instrument", limit=120)
    normalized_measurement = _measurement_payload(measurement_payload)
    normalized_notes = _optional_text(notes, "notes", limit=4000)
    normalized_observed_at = _normalized_datetime(observed_at)
    normalized_attachments = _attachment_ids(
        db, attachment_ids, work_order_id=work_order.id
    )
    normalized_client_ref = _coerce_uuid(client_ref, "client_ref")

    claim_payload = _claim_payload(
        verification_scope=normalized_scope,
        outcome=normalized_outcome,
        observed_external_label=normalized_label,
        observed_asset_type=normalized_asset_type,
        observed_asset_id=normalized_asset_id,
        start_endpoint_type=normalized_start_type,
        start_endpoint_ref_id=normalized_start_id,
        end_endpoint_type=normalized_end_type,
        end_endpoint_ref_id=normalized_end_id,
    )
    claim_sha256 = _digest(claim_payload)
    observation_payload = _observation_payload(
        feature=feature,
        work_order=work_order,
        technician_id=technician.id,
        person_id=person_id,
        system_user_id=system_user_id,
        claim_sha256=claim_sha256,
        latitude=normalized_latitude,
        longitude=normalized_longitude,
        accuracy_m=normalized_accuracy,
        instrument=normalized_instrument,
        measurement_payload=normalized_measurement,
        attachment_ids=normalized_attachments,
        notes=normalized_notes,
        observed_at=normalized_observed_at,
    )
    observation_sha256 = _digest(observation_payload)
    existing_client = db.scalar(
        select(FiberTopologyFieldObservation).where(
            FiberTopologyFieldObservation.client_ref == normalized_client_ref
        )
    )
    if existing_client:
        return _assert_exact_replay(existing_client, observation_sha256)
    existing_observation = db.scalar(
        select(FiberTopologyFieldObservation).where(
            FiberTopologyFieldObservation.observation_sha256 == observation_sha256
        )
    )
    if existing_observation:
        return existing_observation

    observation = FiberTopologyFieldObservation(
        staged_feature_id=feature.id,
        feature_content_sha256=feature.content_sha256,
        source_system=feature.batch.source_system,
        source_profile=feature.batch.profile,
        source_asset_type=feature.asset_type,
        source_external_id=feature.external_id,
        work_order_id=work_order.id,
        work_order_public_id=work_order.public_id,
        verification_scope=normalized_scope,
        outcome=normalized_outcome,
        observed_external_label=normalized_label,
        observed_asset_type=normalized_asset_type,
        observed_asset_id=normalized_asset_id,
        start_endpoint_type=normalized_start_type,
        start_endpoint_ref_id=normalized_start_id,
        end_endpoint_type=normalized_end_type,
        end_endpoint_ref_id=normalized_end_id,
        latitude=normalized_latitude,
        longitude=normalized_longitude,
        accuracy_m=normalized_accuracy,
        instrument=normalized_instrument,
        measurement_payload=normalized_measurement,
        attachment_ids=list(normalized_attachments),
        notes=normalized_notes,
        claim_sha256=claim_sha256,
        observation_sha256=observation_sha256,
        client_ref=normalized_client_ref,
        recorded_by_technician_id=technician.id,
        recorded_by_person_id=person_id,
        recorded_by_system_user_id=system_user_id,
        observed_at=normalized_observed_at,
    )
    db.add(observation)
    try:
        db.commit()
        db.refresh(observation)
    except IntegrityError:
        db.rollback()
        concurrent_client = db.scalar(
            select(FiberTopologyFieldObservation).where(
                FiberTopologyFieldObservation.client_ref == normalized_client_ref
            )
        )
        if concurrent_client:
            return _assert_exact_replay(concurrent_client, observation_sha256)
        concurrent_observation = db.scalar(
            select(FiberTopologyFieldObservation).where(
                FiberTopologyFieldObservation.observation_sha256 == observation_sha256
            )
        )
        if concurrent_observation:
            return concurrent_observation
        raise
    return observation


def list_fiber_field_observations(
    db: Session,
    *,
    work_order_id: object,
    staged_feature_id: object | None = None,
) -> list[FiberTopologyFieldObservation]:
    work_order_uuid = _coerce_uuid(work_order_id, "work_order_id")
    statement = select(FiberTopologyFieldObservation).where(
        FiberTopologyFieldObservation.work_order_id == work_order_uuid
    )
    if staged_feature_id is not None:
        statement = statement.where(
            FiberTopologyFieldObservation.staged_feature_id
            == _coerce_uuid(staged_feature_id, "staged_feature_id")
        )
    return list(
        db.scalars(
            statement.order_by(
                FiberTopologyFieldObservation.observed_at.desc(),
                FiberTopologyFieldObservation.id.desc(),
            )
        ).all()
    )


def observation_to_dict(row: FiberTopologyFieldObservation) -> dict[str, object]:
    return {
        "accuracy_m": row.accuracy_m,
        "attachment_ids": list(row.attachment_ids or []),
        "claim_sha256": row.claim_sha256,
        "client_ref": str(row.client_ref),
        "created_at": _timestamp(row.created_at),
        "end_endpoint_ref_id": (
            str(row.end_endpoint_ref_id) if row.end_endpoint_ref_id else None
        ),
        "end_endpoint_type": row.end_endpoint_type,
        "feature_content_sha256": row.feature_content_sha256,
        "instrument": row.instrument,
        "latitude": row.latitude,
        "longitude": row.longitude,
        "measurement_payload": row.measurement_payload or {},
        "notes": row.notes,
        "observation_id": str(row.id),
        "observation_sha256": row.observation_sha256,
        "observed_asset_id": (
            str(row.observed_asset_id) if row.observed_asset_id else None
        ),
        "observed_asset_type": row.observed_asset_type,
        "observed_at": _timestamp(row.observed_at),
        "observed_external_label": row.observed_external_label,
        "outcome": row.outcome,
        "recorded_by_person_id": str(row.recorded_by_person_id),
        "recorded_by_system_user_id": (
            str(row.recorded_by_system_user_id)
            if row.recorded_by_system_user_id
            else None
        ),
        "recorded_by_technician_id": str(row.recorded_by_technician_id),
        "source_asset_type": row.source_asset_type,
        "source_external_id": row.source_external_id,
        "source_profile": row.source_profile,
        "source_system": row.source_system,
        "staged_feature_id": str(row.staged_feature_id),
        "start_endpoint_ref_id": (
            str(row.start_endpoint_ref_id) if row.start_endpoint_ref_id else None
        ),
        "start_endpoint_type": row.start_endpoint_type,
        "verification_scope": row.verification_scope,
        "work_order_id": str(row.work_order_id),
        "work_order_public_id": row.work_order_public_id,
    }


def _source_key_from_feature(
    feature: FiberTopologyStagedFeature,
) -> tuple[str, str, str]:
    identity = feature.external_id or f"feature:{feature.id}"
    return feature.batch.source_system, feature.asset_type, identity


def _source_key_from_observation(
    row: FiberTopologyFieldObservation,
) -> tuple[str, str, str]:
    identity = row.source_external_id or f"feature:{row.staged_feature_id}"
    return row.source_system, row.source_asset_type, identity


def _scope_state(rows: list[FiberTopologyFieldObservation]) -> str:
    claims = {row.claim_sha256 for row in rows}
    outcomes = {row.outcome for row in rows}
    if len(claims) > 1:
        return "conflicting_observations"
    if outcomes == {"agrees"}:
        return "current_agreement"
    if outcomes & {"conflicts", "not_found"}:
        return "current_conflict"
    return "current_inconclusive"


def project_field_verification_evidence(
    db: Session,
    features: Sequence[FiberTopologyStagedFeature],
) -> dict[str, dict[str, object]]:
    """Project current/superseded/conflicting facts for staged feature reads."""

    if not features:
        return {}
    asset_types = {feature.asset_type for feature in features}
    observations = list(
        db.scalars(
            select(FiberTopologyFieldObservation)
            .where(FiberTopologyFieldObservation.source_asset_type.in_(asset_types))
            .order_by(
                FiberTopologyFieldObservation.observed_at,
                FiberTopologyFieldObservation.id,
            )
        ).all()
    )
    observations_by_source: dict[
        tuple[str, str, str], list[FiberTopologyFieldObservation]
    ] = defaultdict(list)
    attachment_id_values: set[uuid.UUID] = set()
    for observation in observations:
        observations_by_source[_source_key_from_observation(observation)].append(
            observation
        )
        for value in observation.attachment_ids or []:
            try:
                attachment_id_values.add(uuid.UUID(str(value)))
            except (TypeError, ValueError):
                continue
    attachments = (
        list(
            db.scalars(
                select(FieldAttachment).where(
                    FieldAttachment.id.in_(attachment_id_values)
                )
            ).all()
        )
        if attachment_id_values
        else []
    )
    attachment_by_id = {str(attachment.id): attachment for attachment in attachments}

    result: dict[str, dict[str, object]] = {}
    for feature in features:
        candidates = observations_by_source.get(_source_key_from_feature(feature), [])
        current = [
            row
            for row in candidates
            if row.feature_content_sha256 == feature.content_sha256
        ]
        superseded = [row for row in candidates if row not in current]
        drift_ids: list[str] = []
        current_summaries: list[dict[str, object]] = []
        valid_current: list[FiberTopologyFieldObservation] = []
        for row in current:
            attachment_evidence_current = all(
                (attachment := attachment_by_id.get(str(value))) is not None
                and attachment.is_active
                and attachment.work_order_mirror_id == row.work_order_id
                for value in row.attachment_ids or []
            )
            digest_current = bool(
                _digest(_row_claim_payload(row)) == row.claim_sha256
                and _digest(_row_observation_payload(row)) == row.observation_sha256
            )
            source_evidence_current = bool(
                row.source_system == feature.batch.source_system
                and row.source_profile == feature.batch.profile
                and row.source_asset_type == feature.asset_type
                and row.source_external_id == feature.external_id
            )
            evidence_current = bool(
                attachment_evidence_current
                and digest_current
                and source_evidence_current
            )
            if evidence_current:
                valid_current.append(row)
            else:
                drift_ids.append(str(row.id))
            current_summaries.append(
                {
                    "attachment_evidence_current": attachment_evidence_current,
                    "claim_sha256": row.claim_sha256,
                    "evidence_current": evidence_current,
                    "observation_id": str(row.id),
                    "observation_sha256": row.observation_sha256,
                    "observed_at": _timestamp(row.observed_at),
                    "outcome": row.outcome,
                    "verification_scope": row.verification_scope,
                    "work_order_id": str(row.work_order_id),
                    "work_order_public_id": row.work_order_public_id,
                }
            )
        rows_by_scope: dict[str, list[FiberTopologyFieldObservation]] = defaultdict(
            list
        )
        for row in valid_current:
            rows_by_scope[row.verification_scope].append(row)
        scope_states = {
            scope: _scope_state(rows) for scope, rows in sorted(rows_by_scope.items())
        }
        if drift_ids:
            state = "evidence_drift"
        elif not current:
            state = "superseded_only" if superseded else "unobserved"
        elif any(
            value == "conflicting_observations" for value in scope_states.values()
        ):
            state = "conflicting_observations"
        elif any(value == "current_conflict" for value in scope_states.values()):
            state = "current_conflict"
        elif scope_states and all(
            value == "current_agreement" for value in scope_states.values()
        ):
            state = "current_agreement"
        else:
            state = "current_inconclusive"
        superseded_summaries = [
            {
                "feature_content_sha256": row.feature_content_sha256,
                "observation_id": str(row.id),
                "observed_at": _timestamp(row.observed_at),
                "outcome": row.outcome,
                "verification_scope": row.verification_scope,
                "work_order_id": str(row.work_order_id),
                "work_order_public_id": row.work_order_public_id,
            }
            for row in superseded
        ]
        result[str(feature.id)] = {
            "current_observation_count": len(current),
            "current_observations": current_summaries,
            "drift_observation_ids": drift_ids,
            "latest_observed_at": (
                _timestamp(max(row.observed_at for row in current)) if current else None
            ),
            "scope_states": scope_states,
            "state": state,
            "superseded_observation_count": len(superseded),
            "superseded_observation_ids": [str(row.id) for row in superseded],
            "superseded_observations": superseded_summaries,
        }
    return result


def field_verification_state_counts(
    evidence: Mapping[str, Mapping[str, object]],
) -> dict[str, int]:
    counter = Counter(str(row.get("state")) for row in evidence.values())
    return {state: counter[state] for state in PROJECTION_STATES}


__all__ = [
    "OBSERVATION_OUTCOMES",
    "POINT_ASSET_TYPES",
    "PROJECTION_STATES",
    "SOURCE_ASSET_TYPES",
    "VERIFICATION_SCOPES",
    "FiberTopologyFieldObservationError",
    "field_verification_state_counts",
    "list_fiber_field_observations",
    "observation_to_dict",
    "project_field_verification_evidence",
    "record_fiber_field_observation",
]
