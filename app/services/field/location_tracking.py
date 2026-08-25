"""Typed, replay-safe field position observation collection.

This is the qualifying product implementation for the product-first
``dotmac-positioning`` extraction. It owns location evidence and the current
position projection only. Work-order, dispatch, attendance, customer-sharing,
and other business consequences remain product-owned concerns.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile
from app.models.domain_settings import SettingDomain
from app.models.field_location import (
    FieldTechLocationPing,
    FieldTechPresence,
)
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.field.jobs import _profile_from_principal
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.settings_spec import resolve_integer

_OWNER = "operations.position_observations"
_RECORD_BATCH = OwnerCommandDefinition(
    owner=_OWNER,
    concern="field position observations",
    name="record_field_position_observation_batch",
)
_UPDATE_COLLECTION = OwnerCommandDefinition(
    owner=_OWNER,
    concern="field location collection grant",
    name="update_field_location_collection_grant",
)
_PRUNE_OBSERVATIONS = OwnerCommandDefinition(
    owner=_OWNER,
    concern="position observation retention",
    name="prune_expired_position_observations",
)


class ObservationDisposition(StrEnum):
    RECORDED = "recorded"
    REPLAYED = "replayed"


@dataclass(frozen=True)
class LocationPrincipal:
    """Typed identity evidence supplied by the authenticated API adapter."""

    principal_id: str
    person_id: str | None = None
    subscriber_id: str | None = None
    crm_person_id: str | None = None

    @classmethod
    def from_auth(cls, auth: Mapping[str, object]) -> LocationPrincipal:
        principal_id = str(auth.get("principal_id") or "").strip()
        if not principal_id:
            raise DomainError(
                code="position_observation_invalid_principal",
                message="Authenticated location principal is required.",
            )

        def optional_text(name: str) -> str | None:
            value = auth.get(name)
            normalized = str(value).strip() if value is not None else ""
            return normalized or None

        return cls(
            principal_id=principal_id,
            person_id=optional_text("person_id"),
            subscriber_id=optional_text("subscriber_id"),
            crm_person_id=optional_text("crm_person_id"),
        )

    def as_legacy_principal(self) -> dict[str, str]:
        values = {
            "principal_id": self.principal_id,
            "person_id": self.person_id,
            "subscriber_id": self.subscriber_id,
            "crm_person_id": self.crm_person_id,
        }
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True)
class PositionObservation:
    client_observation_id: UUID
    latitude: float
    longitude: float
    accuracy_m: float
    captured_at: datetime
    context_ref: str | None = None
    source: str = "mobile"


@dataclass(frozen=True)
class PositionObservationPolicy:
    """Product-resolved operational bounds supplied to the shared owner seam."""

    max_batch_size: int
    max_future_skew: timedelta
    max_accuracy_m: float


@dataclass(frozen=True)
class RecordLocationBatchCommand:
    context: CommandContext
    principal: LocationPrincipal
    purpose: str
    policy: PositionObservationPolicy
    observations: tuple[PositionObservation, ...]


@dataclass(frozen=True)
class UpdateLocationCollectionCommand:
    context: CommandContext
    principal: LocationPrincipal
    enabled: bool
    purpose: str
    expires_at: datetime | None = None


@dataclass(frozen=True)
class PrunePositionObservationsCommand:
    context: CommandContext
    older_than_hours: int


@dataclass(frozen=True)
class PositionTrackingSnapshot:
    person_id: UUID
    location_sharing_enabled: bool
    collection_purpose: str | None
    collection_granted_at: datetime | None
    collection_expires_at: datetime | None
    last_latitude: float | None
    last_longitude: float | None
    last_location_accuracy_m: float | None
    last_location_at: datetime | None
    last_seen_at: datetime | None


@dataclass(frozen=True)
class PositionObservationError:
    index: int
    client_observation_id: UUID | None
    code: str
    detail: str


@dataclass(frozen=True)
class LocationBatchOutcome:
    accepted: int
    replayed: int
    errors: tuple[PositionObservationError, ...]
    tracking: PositionTrackingSnapshot
    transitions: tuple[object, ...] = ()


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _error(code: str, message: str, **details: object) -> DomainError:
    return DomainError(code=code, message=message, details=details)


def _normalize_source(source: str) -> str:
    normalized = source.strip().lower()
    if not normalized or len(normalized) > 32:
        raise _error(
            "position_observation_invalid_source",
            "Location observation source must contain 1 to 32 characters.",
        )
    return normalized


def _normalize_purpose(purpose: str) -> str:
    normalized = purpose.strip().lower()
    if not normalized or len(normalized) > 32:
        raise _error(
            "position_observation_invalid_collection_purpose",
            "Location collection purpose must contain 1 to 32 characters.",
        )
    return normalized


def _validate_policy(policy: PositionObservationPolicy) -> None:
    if (
        policy.max_batch_size < 1
        or policy.max_future_skew < timedelta(0)
        or policy.max_accuracy_m <= 0
    ):
        raise _error(
            "position_observation_invalid_policy",
            "Location observation policy bounds must be positive.",
        )


def _validate_observation(
    observation: PositionObservation,
    *,
    received_at: datetime,
    policy: PositionObservationPolicy,
) -> tuple[datetime, str, str | None]:
    if not -90.0 <= observation.latitude <= 90.0:
        raise _error(
            "position_observation_invalid_coordinates",
            "Location observation latitude is out of range.",
        )
    if not -180.0 <= observation.longitude <= 180.0:
        raise _error(
            "position_observation_invalid_coordinates",
            "Location observation longitude is out of range.",
        )
    if not 0.0 <= observation.accuracy_m <= policy.max_accuracy_m:
        raise _error(
            "position_observation_invalid_accuracy",
            "Location observation accuracy is out of range.",
        )
    captured_at = _as_utc(observation.captured_at)
    if captured_at > received_at + policy.max_future_skew:
        raise _error(
            "position_observation_future_timestamp",
            "Location observation timestamp is too far in the future.",
        )
    context_ref = (observation.context_ref or "").strip()
    if len(context_ref) > 64:
        raise _error(
            "position_observation_invalid_context",
            "Location observation context reference is too long.",
        )
    return (
        captured_at,
        _normalize_source(observation.source),
        context_ref or None,
    )


def _fingerprint(
    observation: PositionObservation,
    *,
    captured_at: datetime,
    source: str,
    context_ref: str | None,
) -> str:
    payload = {
        "accuracy_m": float(observation.accuracy_m),
        "captured_at": captured_at.isoformat(),
        "latitude": float(observation.latitude),
        "longitude": float(observation.longitude),
        "source": source,
        "context_ref": context_ref,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _tracking_snapshot(presence: FieldTechPresence) -> PositionTrackingSnapshot:
    return PositionTrackingSnapshot(
        person_id=presence.person_id,
        location_sharing_enabled=presence.location_sharing_enabled,
        collection_purpose=presence.collection_purpose,
        collection_granted_at=presence.collection_granted_at,
        collection_expires_at=presence.collection_expires_at,
        last_latitude=presence.last_latitude,
        last_longitude=presence.last_longitude,
        last_location_accuracy_m=presence.last_location_accuracy_m,
        last_location_at=presence.last_location_at,
        last_seen_at=presence.last_seen_at,
    )


def _profile(db: Session, principal: LocationPrincipal) -> TechnicianProfile:
    try:
        return _profile_from_principal(db, principal.as_legacy_principal())
    except HTTPException as exc:
        raise _error(
            "position_observation_technician_not_found",
            "Active technician profile was not found.",
        ) from exc


def _get_or_create_presence(
    db: Session,
    profile: TechnicianProfile,
) -> FieldTechPresence:
    presence = (
        db.query(FieldTechPresence)
        .filter(FieldTechPresence.technician_id == profile.id)
        .one_or_none()
    )
    if presence is None:
        presence = FieldTechPresence(
            technician_id=profile.id,
            person_id=profile.person_id,
        )
        db.add(presence)
        db.flush()
    return presence


class FieldLocationTracking:
    @staticmethod
    def get_tracking(
        db: Session,
        principal: LocationPrincipal,
    ) -> PositionTrackingSnapshot:
        profile = _profile(db, principal)
        presence = (
            db.query(FieldTechPresence)
            .filter(FieldTechPresence.technician_id == profile.id)
            .one_or_none()
        )
        if presence is None:
            return PositionTrackingSnapshot(
                person_id=profile.person_id,
                location_sharing_enabled=False,
                collection_purpose=None,
                collection_granted_at=None,
                collection_expires_at=None,
                last_latitude=None,
                last_longitude=None,
                last_location_accuracy_m=None,
                last_location_at=None,
                last_seen_at=None,
            )
        return _tracking_snapshot(presence)

    @staticmethod
    def update_collection(
        db: Session,
        command: UpdateLocationCollectionCommand,
    ) -> PositionTrackingSnapshot:
        purpose = _normalize_purpose(command.purpose)

        def operation() -> PositionTrackingSnapshot:
            profile = _profile(db, command.principal)
            presence = _get_or_create_presence(db, profile)
            now = _now()
            presence.location_sharing_enabled = command.enabled
            if command.enabled:
                if command.expires_at is None:
                    raise _error(
                        "position_observation_invalid_collection_grant",
                        "Location collection grant expiry is required.",
                    )
                expires_at = _as_utc(command.expires_at)
                if expires_at <= now:
                    raise _error(
                        "position_observation_invalid_collection_grant",
                        "Location collection grant expiry must be in the future.",
                    )
                presence.collection_purpose = purpose
                presence.collection_granted_at = now
                presence.collection_expires_at = expires_at
            else:
                presence.collection_purpose = None
                presence.collection_granted_at = None
                presence.collection_expires_at = None
            presence.last_seen_at = now
            db.flush()
            emit_event(
                db,
                EventType.position_collection_changed,
                {
                    "technician_id": str(profile.id),
                    "enabled": command.enabled,
                    "purpose": purpose,
                },
                actor=command.context.actor,
            )
            return _tracking_snapshot(presence)

        return execute_owner_command(
            db,
            definition=_UPDATE_COLLECTION,
            context=command.context,
            operation=operation,
        )

    @staticmethod
    def record_batch(
        db: Session,
        command: RecordLocationBatchCommand,
    ) -> LocationBatchOutcome:
        _validate_policy(command.policy)
        purpose = _normalize_purpose(command.purpose)
        if not command.observations:
            raise _error(
                "position_observation_empty_batch",
                "Location observation batch cannot be empty.",
            )
        if len(command.observations) > command.policy.max_batch_size:
            raise _error(
                "position_observation_batch_too_large",
                "Location observation batch exceeds the configured limit.",
                max_batch_size=command.policy.max_batch_size,
            )

        def operation() -> LocationBatchOutcome:
            profile = _profile(db, command.principal)
            presence = _get_or_create_presence(db, profile)
            grant_expires_at = presence.collection_expires_at
            if grant_expires_at is not None:
                grant_expires_at = _as_utc(grant_expires_at)
            if (
                not presence.location_sharing_enabled
                or presence.collection_purpose != purpose
                or grant_expires_at is None
                or grant_expires_at <= _now()
            ):
                raise _error(
                    "position_observation_collection_not_granted",
                    "An active collection grant for the requested purpose is required.",
                )
            accepted = 0
            replayed = 0
            errors: list[PositionObservationError] = []

            for index, observation in enumerate(command.observations):
                received_at = _now()
                try:
                    captured_at, source, context_ref = _validate_observation(
                        observation,
                        received_at=received_at,
                        policy=command.policy,
                    )
                    fingerprint = _fingerprint(
                        observation,
                        captured_at=captured_at,
                        source=source,
                        context_ref=context_ref,
                    )
                    existing = (
                        db.query(FieldTechLocationPing)
                        .filter(
                            FieldTechLocationPing.technician_id == profile.id,
                            FieldTechLocationPing.source == source,
                            FieldTechLocationPing.client_observation_id
                            == observation.client_observation_id,
                        )
                        .one_or_none()
                    )
                    if existing is not None:
                        if existing.payload_fingerprint != fingerprint:
                            raise _error(
                                "position_observation_identity_collision",
                                "Location observation identity was reused with different evidence.",
                            )
                        replayed += 1
                        continue

                    ping = FieldTechLocationPing(
                        technician_id=profile.id,
                        person_id=profile.person_id,
                        client_observation_id=observation.client_observation_id,
                        payload_fingerprint=fingerprint,
                        work_order_id=context_ref,
                        latitude=float(observation.latitude),
                        longitude=float(observation.longitude),
                        accuracy_m=float(observation.accuracy_m),
                        captured_at=captured_at,
                        received_at=received_at,
                        source=source,
                    )
                    db.add(ping)

                    presence.last_seen_at = received_at
                    prior = presence.last_location_at
                    if prior is not None:
                        prior = _as_utc(prior)
                    same_time_better_accuracy = prior == captured_at and (
                        presence.last_location_accuracy_m is None
                        or observation.accuracy_m < presence.last_location_accuracy_m
                    )
                    if (
                        prior is None
                        or captured_at > prior
                        or same_time_better_accuracy
                    ):
                        presence.last_latitude = float(observation.latitude)
                        presence.last_longitude = float(observation.longitude)
                        presence.last_location_accuracy_m = float(
                            observation.accuracy_m
                        )
                        presence.last_location_at = captured_at
                    db.flush()
                    emit_event(
                        db,
                        EventType.position_observation_recorded,
                        {
                            "observation_id": str(ping.id),
                            "technician_id": str(profile.id),
                        },
                        actor=command.context.actor,
                    )
                    accepted += 1
                except DomainError as exc:
                    errors.append(
                        PositionObservationError(
                            index=index,
                            client_observation_id=observation.client_observation_id,
                            code=exc.code,
                            detail=exc.message,
                        )
                    )

            return LocationBatchOutcome(
                accepted=accepted,
                replayed=replayed,
                errors=tuple(errors),
                tracking=_tracking_snapshot(presence),
            )

        try:
            return execute_owner_command(
                db,
                definition=_RECORD_BATCH,
                context=command.context,
                operation=operation,
            )
        except IntegrityError:
            # A concurrent exact replay can lose the unique-key race after its
            # initial lookup. The failed owner boundary has already rolled back,
            # so retry once and prove equivalence against the committed winner.
            # Reused identity with changed evidence still becomes a per-item
            # collision through ``operation``.
            return execute_owner_command(
                db,
                definition=_RECORD_BATCH,
                context=command.context,
                operation=operation,
            )

    @staticmethod
    def collection_lease_minutes(db: Session) -> int:
        return resolve_integer(
            db,
            SettingDomain.field,
            "location_collection_lease_minutes",
        )

    @staticmethod
    def retention_hours(db: Session) -> int:
        return resolve_integer(
            db,
            SettingDomain.field,
            "location_ping_retention_hours",
        )

    @staticmethod
    def prune_observations(
        db: Session,
        command: PrunePositionObservationsCommand,
    ) -> int:
        if command.older_than_hours < 1:
            raise _error(
                "position_observation_invalid_retention",
                "Location observation retention must be at least one hour.",
            )

        def operation() -> int:
            cutoff = _now() - timedelta(hours=command.older_than_hours)
            deleted = (
                db.query(FieldTechLocationPing)
                .filter(FieldTechLocationPing.received_at < cutoff)
                .delete(synchronize_session=False)
            )
            deleted_count = int(deleted or 0)
            if deleted_count:
                emit_event(
                    db,
                    EventType.position_observation_pruned,
                    {
                        "deleted_count": deleted_count,
                        "retention_hours": command.older_than_hours,
                    },
                    actor=command.context.actor,
                )
            return deleted_count

        return execute_owner_command(
            db,
            definition=_PRUNE_OBSERVATIONS,
            context=command.context,
            operation=operation,
        )


field_location_tracking = FieldLocationTracking()
