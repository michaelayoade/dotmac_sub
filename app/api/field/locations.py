from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.field import (
    FieldPresenceRead,
    FieldRouteResponse,
    LocationIngestError,
    LocationIngestResponse,
    LocationPingBatch,
    LocationSharingUpdate,
)
from app.services.auth_dependencies import require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.field.location_policy import field_location_policy
from app.services.field.location_tracking import (
    LocationBatchOutcome,
    LocationPrincipal,
    PositionObservation,
    PositionTrackingSnapshot,
    RecordLocationBatchCommand,
    UpdateLocationCollectionCommand,
    field_location_tracking,
)
from app.services.field.presence import (
    FIELD_OPERATIONS_COLLECTION_PURPOSE,
    FieldPresenceStatusSnapshot,
    UpdateFieldPresenceStatusCommand,
    field_presence,
)
from app.services.field.routing import field_routing
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/locations", tags=["field-locations"])


def _context(
    auth: dict,
    *,
    scope: str,
    reason: str,
    idempotency_key: str | None = None,
) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=str(auth["principal_id"]),
        scope=scope,
        reason=reason,
        idempotency_key=idempotency_key,
    )


def _location_error(exc: DomainError) -> HTTPException:
    if exc.code.endswith("technician_not_found"):
        status_code = 404
    elif exc.code.endswith("identity_collision"):
        status_code = 409
    elif "invalid" in exc.code or exc.code.endswith(("empty_batch", "too_large")):
        status_code = 422
    else:
        status_code = 409
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


def _presence_read(
    tracking: PositionTrackingSnapshot,
    presence: FieldPresenceStatusSnapshot,
) -> FieldPresenceRead:
    return FieldPresenceRead(
        person_id=tracking.person_id,
        status=presence.status,
        location_sharing_enabled=tracking.location_sharing_enabled,
        collection_purpose=tracking.collection_purpose,
        collection_granted_at=tracking.collection_granted_at,
        collection_expires_at=tracking.collection_expires_at,
        last_latitude=tracking.last_latitude,
        last_longitude=tracking.last_longitude,
        last_location_accuracy_m=tracking.last_location_accuracy_m,
        last_location_at=tracking.last_location_at,
        last_seen_at=tracking.last_seen_at,
    )


def _ingest_response(
    outcome: LocationBatchOutcome,
    presence: FieldPresenceStatusSnapshot,
) -> LocationIngestResponse:
    return LocationIngestResponse(
        accepted=outcome.accepted,
        replayed=outcome.replayed,
        errors=[
            LocationIngestError(
                index=item.index,
                client_observation_id=item.client_observation_id,
                code=item.code,
                detail=item.detail,
            )
            for item in outcome.errors
        ],
        presence=_presence_read(outcome.tracking, presence),
        transitions=[],
    )


@router.post("", response_model=LocationIngestResponse)
def ingest_locations(
    payload: LocationPingBatch,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    principal = LocationPrincipal.from_auth(auth)
    first_observation_id = payload.pings[0].client_observation_id
    command = RecordLocationBatchCommand(
        context=_context(
            auth,
            scope=str(first_observation_id),
            reason="field_location_batch_ingest",
            idempotency_key=(
                f"field-position-batch:{first_observation_id}:{len(payload.pings)}"
            ),
        ),
        principal=principal,
        purpose=FIELD_OPERATIONS_COLLECTION_PURPOSE,
        policy=field_location_policy(db),
        observations=tuple(
            PositionObservation(
                client_observation_id=ping.client_observation_id,
                latitude=ping.latitude,
                longitude=ping.longitude,
                accuracy_m=ping.accuracy_m,
                captured_at=ping.captured_at,
                context_ref=ping.work_order_id,
                source=ping.source,
            )
            for ping in payload.pings
        ),
    )
    db_session_adapter.release_read_transaction(db)
    try:
        outcome = field_location_tracking.record_batch(db, command)
        presence = field_presence.get_status(db, principal)
        return _ingest_response(outcome, presence)
    except DomainError as exc:
        raise _location_error(exc) from exc


@router.put("/sharing", response_model=FieldPresenceRead)
def update_sharing(
    payload: LocationSharingUpdate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    principal = LocationPrincipal.from_auth(auth)
    lease_expires_at = None
    if payload.enabled:
        lease_minutes = field_location_tracking.collection_lease_minutes(db)
        lease_expires_at = datetime.now(UTC) + timedelta(minutes=lease_minutes)
    collection_command = UpdateLocationCollectionCommand(
        context=_context(
            auth,
            scope=str(auth["principal_id"]),
            reason="field_location_collection_update",
        ),
        principal=principal,
        enabled=payload.enabled,
        purpose=FIELD_OPERATIONS_COLLECTION_PURPOSE,
        expires_at=lease_expires_at,
    )
    db_session_adapter.release_read_transaction(db)
    try:
        if not payload.enabled:
            tracking = field_location_tracking.update_collection(
                db,
                collection_command,
            )
        if payload.status is not None:
            presence = field_presence.update_status(
                db,
                UpdateFieldPresenceStatusCommand(
                    context=_context(
                        auth,
                        scope=str(auth["principal_id"]),
                        reason="field_presence_status_update",
                    ),
                    principal=principal,
                    status=payload.status,
                ),
            )
        else:
            presence = field_presence.get_status(db, principal)
            db_session_adapter.release_read_transaction(db)
        if payload.enabled:
            tracking = field_location_tracking.update_collection(
                db,
                collection_command,
            )
        return _presence_read(tracking, presence)
    except DomainError as exc:
        raise _location_error(exc) from exc


@router.get("/me", response_model=FieldPresenceRead)
def get_my_presence(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    principal = LocationPrincipal.from_auth(auth)
    try:
        return _presence_read(
            field_location_tracking.get_tracking(db, principal),
            field_presence.get_status(db, principal),
        )
    except DomainError as exc:
        raise _location_error(exc) from exc


@router.get("/route", response_model=FieldRouteResponse)
def my_day_route(
    start_lat: float = Query(ge=-90, le=90),
    start_lng: float = Query(ge=-180, le=180),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return {
        "route": field_routing.order_day_route(
            db,
            auth,
            start_latitude=start_lat,
            start_longitude=start_lng,
        )
    }
