"""Governed KMZ admission and permission-scoped Network Map export."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID
from xml.etree import (
    ElementTree as XML,  # nosec B405 - used only for trusted KMZ serialization; uploaded KML is parsed with defusedxml in fiber_topology_staging
)
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from defusedxml.common import DefusedXmlException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.fiber_topology_staging import (
    FiberTopologyFeatureClassificationReview,
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.schemas.network_map_transfer import (
    MAX_REVIEW_FEATURES,
    NetworkMapCoordinate,
    NetworkMapExportLayer,
    NetworkMapExportScope,
    NetworkMapGeometryType,
    NetworkMapImportAssetType,
    NetworkMapImportedFeature,
    NetworkMapImportedGeometry,
    NetworkMapImportMatchStatus,
    NetworkMapImportProfile,
    NetworkMapImportProposalEligibility,
    NetworkMapImportStatus,
    NetworkMapKmzExportOutcome,
    NetworkMapKmzExportQuery,
    NetworkMapKmzImportOutcome,
    ReviewNetworkMapImportFeaturesCommand,
    StageNetworkMapKmzCommand,
)
from app.services import network_map
from app.services.audit_adapter import AuditActor, AuditRecord, audit_adapter
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network import fiber_topology_staging
from app.services.network_map_contracts import (
    NetworkMapFeature,
    NetworkMapFeatureType,
    NetworkMapPointGeometry,
)
from app.services.owner_commands import OwnerCommandDefinition, execute_owner_command

OWNER = "network.map_kmz_transfer"
IMPORT_PERMISSION = "network:fiber:import"
EXPORT_PERMISSION = "network:map:export"
CUSTOMER_PERMISSION = "customer:read"
IMPORT_CONCERN = "administrative KML/KMZ source admission and staging coordination"
CLASSIFICATION_CONCERN = "administrative staged map feature classification review"
EXPORT_CONCERN = "permission-scoped Network Map KMZ export"
MAX_UPLOAD_BYTES = fiber_topology_staging.MAX_KMZ_BYTES
MAX_PREVIEW_FEATURES = 5_000

_IMPORT = OwnerCommandDefinition(
    owner=OWNER,
    concern=IMPORT_CONCERN,
    name="stage_network_map_kmz",
)
_CLASSIFY = OwnerCommandDefinition(
    owner=OWNER,
    concern=CLASSIFICATION_CONCERN,
    name="review_network_map_import_features",
)

_KML_NS = "http://www.opengis.net/kml/2.2"


class NetworkMapTransferError(DomainError):
    """Stable transport-neutral refusal from the KMZ transfer owner."""


def _proposal_eligibility(
    plan: fiber_topology_staging.FiberFeatureMatchPlan,
) -> NetworkMapImportProposalEligibility:
    """Project import evidence into the existing point proposal workflow."""
    feature = plan.feature
    if plan.match_status == "blocked" or feature.blocker_codes:
        return NetworkMapImportProposalEligibility.blocked
    if plan.match_status != "new":
        return NetworkMapImportProposalEligibility.matched
    proposal_types = {
        fiber_topology_staging.FiberAssetType.fiber_access_point,
        fiber_topology_staging.FiberAssetType.fdh_cabinet,
        fiber_topology_staging.FiberAssetType.splice_closure,
        fiber_topology_staging.FiberAssetType.support_structure,
    }
    if feature.asset_type not in proposal_types:
        return NetworkMapImportProposalEligibility.unsupported_asset_type
    if feature.geometry_type != "Point":
        return NetworkMapImportProposalEligibility.non_point_geometry
    if (
        feature.asset_type is fiber_topology_staging.FiberAssetType.support_structure
        and feature.external_id is not None
        and len(feature.external_id) > 80
    ):
        return NetworkMapImportProposalEligibility.source_id_too_long
    if (
        feature.asset_type is fiber_topology_staging.FiberAssetType.support_structure
        and not feature.external_id
    ):
        return NetworkMapImportProposalEligibility.source_id_required
    return NetworkMapImportProposalEligibility.eligible


def _error(code: str, message: str, **details: object) -> NetworkMapTransferError:
    return NetworkMapTransferError(
        code=f"{OWNER}.{code}",
        message=message,
        details=details,
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_text(value: str | None) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise _error(
            "idempotency_key_required",
            "An idempotency key is required for this import.",
        )
    return _sha256(normalized.encode("utf-8"))


def _fingerprint(command: StageNetworkMapKmzCommand, filename: str) -> str:
    payload = json.dumps(
        {
            "actor_id": str(command.actor_id),
            "filename": filename,
            "file_sha256": _sha256(command.content),
            "profile": command.profile.value,
            "reason": command.context.reason.strip(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(payload)


def _coordinates_from_geojson(value: object) -> tuple[NetworkMapCoordinate, ...]:
    if not isinstance(value, list):
        return ()
    coordinates: list[NetworkMapCoordinate] = []
    for item in value:
        if (
            isinstance(item, list)
            and len(item) >= 2
            and isinstance(item[0], int | float)
            and isinstance(item[1], int | float)
        ):
            coordinates.append(
                NetworkMapCoordinate(
                    longitude=float(item[0]),
                    latitude=float(item[1]),
                )
            )
    return tuple(coordinates)


def _imported_geometry(value: dict) -> NetworkMapImportedGeometry:
    raw_type = str(value.get("type") or "GeometryCollection")
    try:
        geometry_type = NetworkMapGeometryType(raw_type)
    except ValueError:
        geometry_type = NetworkMapGeometryType.geometry_collection
    raw_coordinates = value.get("coordinates")
    source: object
    rings: tuple[tuple[NetworkMapCoordinate, ...], ...] = ()
    components: tuple[NetworkMapImportedGeometry, ...] = ()
    if geometry_type is NetworkMapGeometryType.geometry_collection:
        raw_components = value.get("geometries")
        if isinstance(raw_components, list):
            components = tuple(
                _imported_geometry(component)
                for component in raw_components
                if isinstance(component, dict)
            )
        return NetworkMapImportedGeometry(
            geometry_type=geometry_type,
            coordinates=(),
            components=components,
        )
    if geometry_type is NetworkMapGeometryType.point:
        source = [raw_coordinates] if isinstance(raw_coordinates, list) else []
    elif geometry_type is NetworkMapGeometryType.polygon:
        raw_rings = raw_coordinates if isinstance(raw_coordinates, list) else []
        rings = tuple(
            _coordinates_from_geojson(ring)
            for ring in raw_rings
            if isinstance(ring, list)
        )
        source = raw_rings[0] if raw_rings else []
    else:
        source = raw_coordinates
    coordinates = _coordinates_from_geojson(source)
    if geometry_type is NetworkMapGeometryType.point and not coordinates:
        geometry_type = NetworkMapGeometryType.geometry_collection
    return NetworkMapImportedGeometry(
        geometry_type=geometry_type,
        coordinates=coordinates,
        rings=rings,
        components=components,
    )


def _imported_feature(
    plan: fiber_topology_staging.FiberFeatureMatchPlan,
    staged_feature_id: UUID,
) -> NetworkMapImportedFeature:
    feature = plan.feature
    raw_description = feature.source_properties.get("description")
    description = raw_description if isinstance(raw_description, str) else None
    return NetworkMapImportedFeature(
        staged_feature_id=staged_feature_id,
        row_number=feature.row_number,
        asset_type=NetworkMapImportAssetType(feature.asset_type.value),
        external_id=feature.external_id,
        display_name=feature.display_name,
        geometry=_imported_geometry(feature.geometry_geojson),
        match_status=NetworkMapImportMatchStatus(plan.match_status),
        proposal_eligibility=_proposal_eligibility(plan),
        blocker_codes=feature.blocker_codes,
        match_reasons=plan.match_reasons,
        candidate_asset_ids=plan.candidate_asset_ids,
        description=description,
        suggested_asset_type=(
            NetworkMapImportAssetType(feature.suggested_asset_type.value)
            if feature.suggested_asset_type is not None
            else None
        ),
        resource_warnings=(
            tuple(str(feature.source_properties["resource_warnings"]).split("|"))
            if feature.source_properties.get("resource_warnings")
            else ()
        ),
    )


def _outcome(
    *,
    db: Session,
    batch: FiberTopologySourceBatch,
    profile: NetworkMapImportProfile,
    preview: fiber_topology_staging.FiberSourcePreview,
    created: bool,
) -> NetworkMapKmzImportOutcome:
    plans = (
        _effective_review_plans(db, batch)
        if profile is NetworkMapImportProfile.mixed_network_map
        else preview.features
    )
    return _outcome_from_plans(
        db=db,
        batch=batch,
        profile=profile,
        plans=plans,
        created=created,
    )


def _outcome_from_plans(
    *,
    db: Session,
    batch: FiberTopologySourceBatch,
    profile: NetworkMapImportProfile,
    plans: tuple[fiber_topology_staging.FiberFeatureMatchPlan, ...],
    created: bool,
) -> NetworkMapKmzImportOutcome:
    staged_ids = {
        row.row_number: row.id
        for row in db.scalars(
            select(FiberTopologyStagedFeature)
            .where(FiberTopologyStagedFeature.batch_id == batch.id)
            .order_by(FiberTopologyStagedFeature.row_number)
        ).all()
    }
    status_counts = Counter(plan.match_status for plan in plans)
    blocker_count = status_counts.get("blocked", 0)
    candidate_count = sum(
        status_counts.get(status, 0)
        for status in ("exact_external", "candidate", "ambiguous")
    )
    return NetworkMapKmzImportOutcome(
        batch_id=batch.id,
        created=created,
        status=(
            NetworkMapImportStatus.blocked
            if blocker_count
            else NetworkMapImportStatus.staged
        ),
        profile=profile,
        source_name=batch.source_name,
        file_sha256=batch.file_sha256,
        manifest_sha256=batch.manifest_sha256,
        feature_count=len(plans),
        blocker_count=blocker_count,
        candidate_count=candidate_count,
        new_count=status_counts.get("new", 0),
        unchanged_count=status_counts.get("unchanged", 0),
        unclassified_count=sum(
            plan.feature.asset_type
            is fiber_topology_staging.FiberAssetType.unclassified
            for plan in plans
        ),
        features=tuple(
            _imported_feature(plan, staged_ids[plan.feature.row_number])
            for plan in plans[:MAX_PREVIEW_FEATURES]
        ),
        preview_truncated=len(plans) > MAX_PREVIEW_FEATURES,
    )


def _effective_review_plans(
    db: Session,
    batch: FiberTopologySourceBatch,
) -> tuple[fiber_topology_staging.FiberFeatureMatchPlan, ...]:
    profile = fiber_topology_staging.SOURCE_PROFILES["mixed_network_map"]
    rows = db.scalars(
        select(FiberTopologyStagedFeature)
        .where(FiberTopologyStagedFeature.batch_id == batch.id)
        .order_by(FiberTopologyStagedFeature.row_number)
    ).all()
    latest_reviews: dict[UUID, FiberTopologyFeatureClassificationReview] = {}
    reviews = db.scalars(
        select(FiberTopologyFeatureClassificationReview)
        .where(FiberTopologyFeatureClassificationReview.batch_id == batch.id)
        .order_by(
            FiberTopologyFeatureClassificationReview.staged_feature_id,
            FiberTopologyFeatureClassificationReview.revision.desc(),
        )
    ).all()
    for review in reviews:
        latest_reviews.setdefault(review.staged_feature_id, review)

    classification_blockers = {
        "missing_asset_type",
        "unsupported_asset_type",
        "unexpected_geometry_type",
    }
    parsed: list[fiber_topology_staging.ParsedFiberFeature] = []
    for row in rows:
        latest_review = latest_reviews.get(row.id)
        asset_type = fiber_topology_staging.FiberAssetType(
            latest_review.asset_type if latest_review is not None else row.asset_type
        )
        blocker_codes = list(row.blocker_codes)
        if latest_review is not None:
            blocker_codes = [
                code for code in blocker_codes if code not in classification_blockers
            ]
            if not fiber_topology_staging.mixed_geometry_compatible(
                asset_type, row.geometry_type
            ):
                blocker_codes.append("unexpected_geometry_type")

        suggestion_value = row.source_properties.get("suggested_asset_type")
        try:
            suggested = (
                fiber_topology_staging.FiberAssetType(suggestion_value)
                if isinstance(suggestion_value, str)
                else None
            )
        except ValueError:
            suggested = None
        normalized = {
            "normalization_version": fiber_topology_staging.NORMALIZATION_VERSION,
            "asset_type": asset_type.value,
            "declared_asset_type": row.source_properties.get("dotmac_asset_type"),
            "external_id": row.external_id,
            "display_name": row.display_name,
            "geometry": row.geometry_geojson,
            "properties": row.source_properties,
        }
        parsed.append(
            fiber_topology_staging.ParsedFiberFeature(
                row_number=row.row_number,
                asset_type=asset_type,
                external_id=row.external_id,
                display_name=row.display_name,
                geometry_type=row.geometry_type,
                geometry_geojson=row.geometry_geojson,
                source_properties=row.source_properties,
                content_sha256=_sha256(
                    json.dumps(
                        normalized,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                ),
                geometry_sha256=row.geometry_sha256,
                blocker_codes=tuple(dict.fromkeys(blocker_codes)),
                suggested_asset_type=suggested,
            )
        )
    return fiber_topology_staging.revalidate_mixed_features(db, tuple(parsed))


def review_network_map_import_features(
    db: Session,
    command: ReviewNetworkMapImportFeaturesCommand,
) -> NetworkMapKmzImportOutcome:
    try:
        return execute_owner_command(
            db,
            definition=_CLASSIFY,
            context=command.context,
            operation=lambda: _review_network_map_import_features(db, command),
        )
    except IntegrityError as exc:
        raise _error(
            "review_idempotency_conflict",
            "This classification review conflicts with an existing command.",
        ) from exc


def _review_network_map_import_features(
    db: Session,
    command: ReviewNetworkMapImportFeaturesCommand,
) -> NetworkMapKmzImportOutcome:
    if command.context.scope != IMPORT_PERMISSION:
        raise _error("invalid_scope", "The review has an invalid permission scope.")
    expected_actor = f"{command.actor_type.value}:{command.actor_id}"
    if command.context.actor != expected_actor or not command.actor_label.strip():
        raise _error("invalid_actor", "A valid review actor is required.")
    reason = command.context.reason.strip()
    if not reason:
        raise _error("reason_required", "A review reason is required.")
    if not command.edits or len(command.edits) > MAX_REVIEW_FEATURES:
        raise _error(
            "review_size_invalid",
            f"Choose between 1 and {MAX_REVIEW_FEATURES} feature classifications.",
        )
    feature_ids = tuple(edit.staged_feature_id for edit in command.edits)
    if len(set(feature_ids)) != len(feature_ids):
        raise _error(
            "review_duplicate_feature",
            "Each staged feature can appear only once in a review submission.",
        )
    allowed_types = {
        fiber_topology_staging.FiberAssetType.fiber_segment,
        fiber_topology_staging.FiberAssetType.fiber_access_point,
        fiber_topology_staging.FiberAssetType.fdh_cabinet,
        fiber_topology_staging.FiberAssetType.splice_closure,
        fiber_topology_staging.FiberAssetType.service_building,
        fiber_topology_staging.FiberAssetType.support_structure,
    }
    if any(
        fiber_topology_staging.FiberAssetType(edit.asset_type.value)
        not in allowed_types
        for edit in command.edits
    ):
        raise _error(
            "review_asset_type_invalid",
            "Select one of the supported fiber plant asset types.",
        )
    idempotency_sha256 = _hash_text(command.context.idempotency_key)
    fingerprint_payload = {
        "actor_id": str(command.actor_id),
        "batch_id": str(command.batch_id),
        "features": sorted(
            (str(edit.staged_feature_id), edit.asset_type.value)
            for edit in command.edits
        ),
        "reason": reason,
    }
    fingerprint_sha256 = _sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    batch = db.scalar(
        select(FiberTopologySourceBatch)
        .where(FiberTopologySourceBatch.id == command.batch_id)
        .with_for_update()
    )
    if (
        batch is None
        or batch.profile != NetworkMapImportProfile.mixed_network_map.value
    ):
        raise _error("batch_not_found", "The staged map import could not be found.")
    existing_rows = db.scalars(
        select(FiberTopologyFeatureClassificationReview).where(
            FiberTopologyFeatureClassificationReview.batch_id == batch.id,
            FiberTopologyFeatureClassificationReview.command_key_sha256
            == idempotency_sha256,
        )
    ).all()
    if existing_rows:
        if any(
            row.command_fingerprint_sha256 != fingerprint_sha256
            for row in existing_rows
        ):
            raise _error(
                "review_idempotency_conflict",
                "The review key was reused with different feature or type choices.",
            )
        plans = _effective_review_plans(db, batch)
        return _outcome_from_plans(
            db=db,
            batch=batch,
            profile=NetworkMapImportProfile.mixed_network_map,
            plans=plans,
            created=False,
        )

    staged_rows = db.scalars(
        select(FiberTopologyStagedFeature)
        .where(
            FiberTopologyStagedFeature.batch_id == batch.id,
            FiberTopologyStagedFeature.id.in_(feature_ids),
        )
        .order_by(FiberTopologyStagedFeature.id)
        .with_for_update()
    ).all()
    if {row.id for row in staged_rows} != set(feature_ids):
        raise _error(
            "review_feature_not_found",
            "One or more selected features do not belong to this staged import.",
        )
    edit_by_id = {edit.staged_feature_id: edit for edit in command.edits}
    revision_rows = db.execute(
        select(
            FiberTopologyFeatureClassificationReview.staged_feature_id,
            func.max(FiberTopologyFeatureClassificationReview.revision),
        )
        .where(
            FiberTopologyFeatureClassificationReview.staged_feature_id.in_(feature_ids)
        )
        .group_by(FiberTopologyFeatureClassificationReview.staged_feature_id)
    ).all()
    revisions = {
        feature_id: int(revision or 0) for feature_id, revision in revision_rows
    }
    for row in staged_rows:
        edit = edit_by_id[row.id]
        db.add(
            FiberTopologyFeatureClassificationReview(
                batch_id=batch.id,
                staged_feature_id=row.id,
                revision=revisions.get(row.id, 0) + 1,
                asset_type=edit.asset_type.value,
                command_key_sha256=idempotency_sha256,
                command_fingerprint_sha256=fingerprint_sha256,
                reviewed_by=command.actor_label.strip()[:160],
                reason=reason[:500],
            )
        )
    db.flush()
    audit_adapter.stage(
        db,
        AuditRecord(
            action="network_map.kmz_import_classification_reviewed",
            entity_type="fiber_topology_source_batch",
            entity_id=str(batch.id),
            actor=AuditActor(
                actor_type=command.actor_type,
                actor_id=str(command.actor_id),
                label=command.actor_label,
            ),
            request_id=str(command.context.correlation_id),
            metadata={
                "owner": OWNER,
                "review_command_sha256": idempotency_sha256,
                "feature_count": len(command.edits),
                "reason": reason,
            },
        ),
    )
    emit_event(
        db,
        EventType.network_map_kmz_classification_reviewed,
        {
            "batch_id": str(batch.id),
            "review_command_sha256": idempotency_sha256,
            "feature_count": len(command.edits),
            "reason": reason,
        },
        actor=command.context.actor,
    )
    plans = _effective_review_plans(db, batch)
    return _outcome_from_plans(
        db=db,
        batch=batch,
        profile=NetworkMapImportProfile.mixed_network_map,
        plans=plans,
        created=True,
    )


def stage_network_map_kmz(
    db: Session,
    command: StageNetworkMapKmzCommand,
) -> NetworkMapKmzImportOutcome:
    try:
        return execute_owner_command(
            db,
            definition=_IMPORT,
            context=command.context,
            operation=lambda: _stage_network_map_kmz(db, command),
        )
    except IntegrityError as exc:
        raise _error(
            "idempotency_conflict",
            "This map import conflicts with an existing import command.",
        ) from exc


def _stage_network_map_kmz(
    db: Session,
    command: StageNetworkMapKmzCommand,
) -> NetworkMapKmzImportOutcome:
    if command.context.scope != IMPORT_PERMISSION:
        raise _error("invalid_scope", "The import has an invalid permission scope.")
    expected_actor = f"{command.actor_type.value}:{command.actor_id}"
    if command.context.actor != expected_actor or not command.actor_label.strip():
        raise _error("invalid_actor", "A valid import actor is required.")
    if not command.context.reason.strip():
        raise _error("reason_required", "An import reason is required.")
    if not command.content:
        raise _error("empty_file", "Choose a KML or KMZ file to import.")
    if len(command.content) > MAX_UPLOAD_BYTES:
        raise _error(
            "file_too_large",
            "The map file exceeds the 25 MB upload limit.",
            maximum_bytes=MAX_UPLOAD_BYTES,
        )
    filename = Path(command.filename).name.strip()
    suffix = Path(filename).suffix.casefold()
    if not filename or suffix not in {".kml", ".kmz"}:
        raise _error(
            "invalid_file_type", "Network Map imports must be KML or KMZ files."
        )
    if len(filename) > 255:
        filename = f"{Path(filename).stem[: 255 - len(suffix)]}{suffix}"
    command_key_sha256 = _hash_text(command.context.idempotency_key)
    fingerprint = _fingerprint(command, filename)
    try:
        preview = fiber_topology_staging.preview_uploaded_fiber_source(
            db,
            content=command.content,
            source_name=filename,
            profile_name=command.profile.value,
        )
    except (ValueError, DefusedXmlException) as exc:
        raise _error(
            "invalid_archive",
            f"{filename}: {exc}",
            source_name=filename,
        ) from exc
    existing_key = db.scalar(
        select(FiberTopologySourceBatch).where(
            FiberTopologySourceBatch.command_key_sha256 == command_key_sha256
        )
    )
    if existing_key is not None:
        if existing_key.command_fingerprint_sha256 != fingerprint:
            raise _error(
                "idempotency_conflict",
                "The import key was reused with different file or profile inputs.",
            )
        return _outcome(
            db=db,
            batch=existing_key,
            profile=command.profile,
            preview=preview,
            created=False,
        )

    existing_manifest = db.scalar(
        select(FiberTopologySourceBatch).where(
            FiberTopologySourceBatch.source_system == preview.source_system,
            FiberTopologySourceBatch.profile == preview.profile.name,
            FiberTopologySourceBatch.manifest_sha256 == preview.manifest_sha256,
        )
    )
    if existing_manifest is not None:
        if existing_manifest.command_key_sha256 is None:
            existing_manifest.command_key_sha256 = command_key_sha256
            existing_manifest.command_fingerprint_sha256 = fingerprint
            db.flush()
        return _outcome(
            db=db,
            batch=existing_manifest,
            profile=command.profile,
            preview=preview,
            created=False,
        )

    result = fiber_topology_staging.persist_fiber_preview(
        db,
        preview,
        plans=preview.features,
        source_name=filename,
        created_by=command.actor_label.strip()[:160],
        source_metadata={
            "admission_owner": OWNER,
            "correlation_id": str(command.context.correlation_id),
            "reason": command.context.reason.strip(),
        },
        command_key_sha256=command_key_sha256,
        command_fingerprint_sha256=fingerprint,
    )
    batch = db.get(FiberTopologySourceBatch, result.batch_id)
    if batch is None:
        raise _error("staging_failed", "The staged import record was not created.")
    audit_adapter.stage(
        db,
        AuditRecord(
            action="network_map.kmz_import_staged",
            entity_type="fiber_topology_source_batch",
            entity_id=str(batch.id),
            actor=AuditActor(
                actor_type=command.actor_type,
                actor_id=str(command.actor_id),
                label=command.actor_label,
            ),
            request_id=str(command.context.correlation_id),
            metadata={
                "owner": OWNER,
                "profile": command.profile.value,
                "file_sha256": batch.file_sha256,
                "manifest_sha256": batch.manifest_sha256,
                "status": batch.status,
                "feature_count": batch.feature_count,
                "blocker_count": batch.blocker_count,
            },
        ),
    )
    emit_event(
        db,
        EventType.network_map_kmz_import_staged,
        {
            "batch_id": str(batch.id),
            "profile": command.profile.value,
            "status": batch.status,
            "file_sha256": batch.file_sha256,
            "manifest_sha256": batch.manifest_sha256,
            "feature_count": batch.feature_count,
            "blocker_count": batch.blocker_count,
        },
        actor=command.context.actor,
    )
    return _outcome(
        db=db,
        batch=batch,
        profile=command.profile,
        preview=preview,
        created=result.created,
    )


def _layer_for(feature: NetworkMapFeature) -> NetworkMapExportLayer:
    feature_type = feature.properties.feature_type
    if feature_type is NetworkMapFeatureType.fiber_segment:
        return NetworkMapExportLayer.fiber
    if feature_type is NetworkMapFeatureType.network_device:
        return NetworkMapExportLayer.network_devices
    if feature_type is NetworkMapFeatureType.ont:
        return NetworkMapExportLayer.onts
    if feature_type is NetworkMapFeatureType.customer:
        return NetworkMapExportLayer.customers
    return NetworkMapExportLayer.infrastructure


def _inside_bounds(feature: NetworkMapFeature, query: NetworkMapKmzExportQuery) -> bool:
    if query.scope is NetworkMapExportScope.all or query.bounds is None:
        return True
    bounds = query.bounds
    geometry = feature.geometry
    points = (
        ((geometry.longitude, geometry.latitude),)
        if isinstance(geometry, NetworkMapPointGeometry)
        else geometry.coordinates
    )
    longitudes = tuple(longitude for longitude, _latitude in points)
    latitudes = tuple(latitude for _longitude, latitude in points)
    return bool(longitudes) and not (
        max(longitudes) < bounds.west
        or min(longitudes) > bounds.east
        or max(latitudes) < bounds.south
        or min(latitudes) > bounds.north
    )


def _matches_filters(
    feature: NetworkMapFeature, query: NetworkMapKmzExportQuery
) -> bool:
    properties = feature.properties
    feature_type = properties.feature_type
    if (
        feature_type is NetworkMapFeatureType.customer
        and query.customer_status is not None
        and properties.customer_status is not query.customer_status
    ):
        return False
    if feature_type is NetworkMapFeatureType.network_device:
        if (
            query.device_status is not None
            and properties.status is not query.device_status
        ):
            return False
        if (
            query.device_type is not None
            and properties.device_type is not query.device_type
        ):
            return False
    if feature_type is NetworkMapFeatureType.ont:
        if query.ont_status is not None and properties.status is not query.ont_status:
            return False
        if (
            query.signal_quality is not None
            and properties.signal_quality is not query.signal_quality
        ):
            return False
    if feature_type is NetworkMapFeatureType.support_structure:
        if (
            query.support_lifecycle is not None
            and properties.lifecycle_status is not query.support_lifecycle
        ):
            return False
        if (
            query.inspection_status is not None
            and properties.inspection_status is not query.inspection_status
        ):
            return False
    return not (
        feature_type is NetworkMapFeatureType.fiber_segment
        and query.segment_type is not None
        and properties.segment_type is not query.segment_type
    )


def _text(parent: XML.Element, name: str, value: object) -> XML.Element:
    element = XML.SubElement(parent, f"{{{_KML_NS}}}{name}")
    element.text = str(value)
    return element


def _extended_data(placemark: XML.Element, feature: NetworkMapFeature) -> None:
    properties = feature.properties
    values: tuple[tuple[str, object | None], ...] = (
        ("dotmac_asset_type", properties.feature_type.value),
        ("dotmac_asset_id", properties.id),
        ("code", properties.code),
        ("city", properties.city),
        ("street", properties.street),
        ("status", properties.status.value if properties.status else None),
        ("status_reason", properties.status_reason),
        (
            "segment_type",
            properties.segment_type.value if properties.segment_type else None,
        ),
        ("cable_type", properties.cable_type.value if properties.cable_type else None),
        ("fiber_count", properties.fiber_count),
        ("length_m", properties.length_m),
        (
            "customer_status",
            properties.customer_status.value if properties.customer_status else None,
        ),
        ("address", properties.address),
        (
            "connectivity",
            properties.connectivity.layer.value if properties.connectivity else None,
        ),
    )
    extended = XML.SubElement(placemark, f"{{{_KML_NS}}}ExtendedData")
    for key, value in values:
        if value is None:
            continue
        data = XML.SubElement(extended, f"{{{_KML_NS}}}Data", {"name": key})
        _text(data, "value", value)


def _geometry_element(placemark: XML.Element, feature: NetworkMapFeature) -> None:
    geometry = feature.geometry
    if isinstance(geometry, NetworkMapPointGeometry):
        point = XML.SubElement(placemark, f"{{{_KML_NS}}}Point")
        _text(
            point, "coordinates", f"{geometry.longitude:.7f},{geometry.latitude:.7f},0"
        )
        return
    line = XML.SubElement(placemark, f"{{{_KML_NS}}}LineString")
    _text(line, "tessellate", 1)
    _text(
        line,
        "coordinates",
        " ".join(
            f"{longitude:.7f},{latitude:.7f},0"
            for longitude, latitude in geometry.coordinates
        ),
    )


def export_network_map_kmz(
    db: Session,
    query: NetworkMapKmzExportQuery,
) -> NetworkMapKmzExportOutcome:
    selected_layers = set(query.layers)
    if not selected_layers:
        raise _error("empty_export", "Select at least one map layer to export.")
    if (
        NetworkMapExportLayer.customers in selected_layers
        and not query.include_customers
    ):
        selected_layers.remove(NetworkMapExportLayer.customers)
    projection = network_map.build_network_map_projection(db=db)
    features = tuple(
        sorted(
            (
                feature
                for feature in projection.features
                if _layer_for(feature) in selected_layers
                and _inside_bounds(feature, query)
                and _matches_filters(feature, query)
            ),
            key=lambda feature: (
                _layer_for(feature).value,
                feature.properties.feature_type.value,
                str(feature.properties.id),
            ),
        )
    )
    root = XML.Element(f"{{{_KML_NS}}}kml")
    document = XML.SubElement(root, f"{{{_KML_NS}}}Document")
    _text(document, "name", "Dotmac Network Map")
    for layer in NetworkMapExportLayer:
        layer_features = tuple(
            feature for feature in features if _layer_for(feature) is layer
        )
        if not layer_features:
            continue
        folder = XML.SubElement(document, f"{{{_KML_NS}}}Folder")
        _text(folder, "name", layer.value.replace("_", " ").title())
        for feature in layer_features:
            placemark = XML.SubElement(folder, f"{{{_KML_NS}}}Placemark")
            _text(placemark, "name", feature.properties.name)
            _extended_data(placemark, feature)
            _geometry_element(placemark, feature)
    kml = XML.tostring(root, encoding="utf-8", xml_declaration=True)
    output = BytesIO()
    info = ZipInfo("doc.kml", date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    with ZipFile(output, mode="w") as archive:
        archive.writestr(info, kml)
    content = output.getvalue()
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return NetworkMapKmzExportOutcome(
        filename=f"network-map-{timestamp}.kmz",
        content=content,
        feature_count=len(features),
        file_sha256=_sha256(content),
    )


__all__ = [
    "CUSTOMER_PERMISSION",
    "EXPORT_PERMISSION",
    "IMPORT_PERMISSION",
    "MAX_UPLOAD_BYTES",
    "MAX_PREVIEW_FEATURES",
    "NetworkMapTransferError",
    "review_network_map_import_features",
    "export_network_map_kmz",
    "stage_network_map_kmz",
]
