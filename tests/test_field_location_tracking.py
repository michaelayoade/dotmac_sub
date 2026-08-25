from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.field_job_event import FieldJobEvent
from app.models.field_location import FieldTechLocationPing
from app.models.subscriber import Subscriber, UserType
from app.models.subscription_engine import SettingValueType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.auth_dependencies import require_user_auth
from app.services.domain_errors import DomainError
from app.services.field import location_tracking as location_tracking_service
from app.services.field.location_tracking import (
    LocationPrincipal,
    PositionObservation,
    PositionObservationPolicy,
    RecordLocationBatchCommand,
    UpdateLocationCollectionCommand,
    field_location_tracking,
)
from app.services.field.presence import (
    FIELD_OPERATIONS_COLLECTION_PURPOSE,
    UpdateFieldPresenceStatusCommand,
    field_presence,
)
from app.services.owner_commands import CommandContext


def _user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Live",
        last_name="Tech",
        display_name="Live Tech",
        email=f"live-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _auth(user: SystemUser) -> dict:
    user_id = inspect(user).identity[0]
    return {
        "principal_id": str(user_id),
        "person_id": str(user_id),
        "subscriber_id": str(user_id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _profile(db_session, user: SystemUser) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id="crm-live-tech",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Live",
        last_name="Customer",
        email=f"live-customer-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(
    db_session,
    subscriber: Subscriber,
    *,
    crm_work_order_id: str = "wo-geofence",
    status: str = "dispatched",
) -> WorkOrder:
    row = WorkOrder(
        crm_work_order_id=crm_work_order_id,
        subscriber_id=subscriber.id,
        title="Geofence install",
        status=status,
        assigned_to_crm_person_id="crm-live-tech",
        address="Plot 14, Jabi",
        scheduled_start=datetime.now(UTC),
        metadata_={"location": {"lat": 9.071, "lng": 7.451}},
    )
    db_session.add(row)
    db_session.flush()
    return row


def _field_setting(db_session, key: str, value: str) -> None:
    db_session.add(
        DomainSetting(
            domain=SettingDomain.field,
            key=key,
            value_type=SettingValueType.boolean,
            value_text=value,
        )
    )


def _context(reason: str, *, scope: str = "field-position") -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="test-technician",
        scope=scope,
        reason=reason,
    )


def _observation(**overrides: object) -> PositionObservation:
    observation_id = overrides.get("client_observation_id", uuid4())
    return PositionObservation(
        client_observation_id=(
            observation_id
            if isinstance(observation_id, UUID)
            else UUID(str(observation_id))
        ),
        latitude=float(overrides.get("latitude", 9.071)),
        longitude=float(overrides.get("longitude", 7.451)),
        accuracy_m=float(overrides.get("accuracy_m", 10)),
        captured_at=overrides.get("captured_at", datetime.now(UTC)),  # type: ignore[arg-type]
        context_ref=overrides.get("work_order_id"),  # type: ignore[arg-type]
        source=str(overrides.get("source", "mobile")),
    )


def _policy(**overrides: object) -> PositionObservationPolicy:
    return PositionObservationPolicy(
        max_batch_size=int(overrides.get("max_batch_size", 200)),
        max_future_skew=overrides.get(  # type: ignore[arg-type]
            "max_future_skew",
            timedelta(minutes=5),
        ),
        max_accuracy_m=float(overrides.get("max_accuracy_m", 1000)),
    )


def _record_batch(
    db_session,
    user: SystemUser,
    observations: list[PositionObservation],
):
    _update_collection(
        db_session,
        user,
        enabled=True,
        status="on_shift",
    )
    return field_location_tracking.record_batch(
        db_session,
        RecordLocationBatchCommand(
            context=_context("test_location_batch"),
            principal=LocationPrincipal.from_auth(_auth(user)),
            purpose=FIELD_OPERATIONS_COLLECTION_PURPOSE,
            policy=_policy(),
            observations=tuple(observations),
        ),
    )


def _update_collection(
    db_session,
    user: SystemUser,
    *,
    enabled: bool,
    status: str | None = None,
):
    principal = LocationPrincipal.from_auth(_auth(user))
    if status is not None:
        field_presence.update_status(
            db_session,
            UpdateFieldPresenceStatusCommand(
                context=_context("test_field_presence_status"),
                principal=principal,
                status=status,
            ),
        )
    return field_location_tracking.update_collection(
        db_session,
        UpdateLocationCollectionCommand(
            context=_context("test_location_collection"),
            principal=principal,
            enabled=enabled,
            purpose=FIELD_OPERATIONS_COLLECTION_PURPOSE,
            expires_at=(datetime.now(UTC) + timedelta(hours=12) if enabled else None),
        ),
    )


def test_record_batch_persists_pings_and_updates_presence(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    now = datetime.now(UTC)
    db_session.commit()

    result = _record_batch(
        db_session,
        user,
        [
            _observation(
                latitude=9.071,
                longitude=7.451,
                accuracy_m=10,
                captured_at=now,
                work_order_id="wo-live",
            ),
            _observation(
                latitude=9.072,
                longitude=7.452,
                accuracy_m=7,
                captured_at=now + timedelta(minutes=1),
            ),
        ],
    )

    assert result.accepted == 2
    assert result.replayed == 0
    assert result.errors == ()
    assert result.tracking.last_latitude == 9.072
    assert (
        field_presence.get_status(
            db_session,
            LocationPrincipal.from_auth(_auth(user)),
        ).status
        == "on_shift"
    )
    assert (
        db_session.query(FieldTechLocationPing)
        .filter(FieldTechLocationPing.work_order_id == "wo-live")
        .count()
        == 1
    )


def test_stale_ping_does_not_roll_presence_backwards(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    now = datetime.now(UTC)
    db_session.commit()

    _record_batch(
        db_session,
        user,
        [_observation(latitude=9.071, longitude=7.451, captured_at=now)],
    )
    _record_batch(
        db_session,
        user,
        [
            _observation(
                latitude=1.0,
                longitude=1.0,
                captured_at=now - timedelta(minutes=5),
            )
        ],
    )

    tracking = field_location_tracking.get_tracking(
        db_session,
        LocationPrincipal.from_auth(_auth(user)),
    )
    assert tracking.last_latitude == 9.071
    assert db_session.query(FieldTechLocationPing).count() == 2


def test_equal_timestamp_only_better_accuracy_advances_presence(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    captured_at = datetime.now(UTC)
    db_session.commit()

    _record_batch(
        db_session,
        user,
        [
            _observation(
                latitude=9.071,
                longitude=7.451,
                accuracy_m=8,
                captured_at=captured_at,
            ),
            _observation(
                latitude=1,
                longitude=1,
                accuracy_m=30,
                captured_at=captured_at,
            ),
            _observation(
                latitude=9.072,
                longitude=7.452,
                accuracy_m=4,
                captured_at=captured_at,
            ),
        ],
    )

    tracking = field_location_tracking.get_tracking(
        db_session,
        LocationPrincipal.from_auth(_auth(user)),
    )
    assert tracking.last_latitude == 9.072
    assert tracking.last_location_accuracy_m == 4


def test_location_batch_collects_per_ping_errors_without_persisting_rejected_row(
    db_session,
):
    user = _user(db_session)
    _profile(db_session, user)
    db_session.commit()

    result = _record_batch(
        db_session,
        user,
        [
            _observation(),
            _observation(latitude=91),
        ],
    )

    assert result.accepted == 1
    assert result.errors[0].index == 1
    assert result.errors[0].code == "position_observation_invalid_coordinates"
    assert db_session.query(FieldTechLocationPing).count() == 1


def test_product_policy_controls_batch_and_accuracy_limits(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    db_session.commit()
    _update_collection(db_session, user, enabled=True, status="on_shift")

    with pytest.raises(DomainError) as exc:
        field_location_tracking.record_batch(
            db_session,
            RecordLocationBatchCommand(
                context=_context("test_product_batch_limit"),
                principal=LocationPrincipal.from_auth(_auth(user)),
                purpose=FIELD_OPERATIONS_COLLECTION_PURPOSE,
                policy=_policy(max_batch_size=1),
                observations=(_observation(), _observation()),
            ),
        )

    assert exc.value.code == "position_observation_batch_too_large"

    result = field_location_tracking.record_batch(
        db_session,
        RecordLocationBatchCommand(
            context=_context("test_product_accuracy_limit"),
            principal=LocationPrincipal.from_auth(_auth(user)),
            purpose=FIELD_OPERATIONS_COLLECTION_PURPOSE,
            policy=_policy(max_accuracy_m=5),
            observations=(_observation(accuracy_m=6),),
        ),
    )
    assert result.accepted == 0
    assert result.errors[0].code == "position_observation_invalid_accuracy"


def test_exact_location_observation_replay_is_idempotent(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    observation = _observation()
    db_session.commit()

    first = _record_batch(db_session, user, [observation])
    second = _record_batch(db_session, user, [observation])

    assert first.accepted == 1
    assert first.replayed == 0
    assert second.accepted == 0
    assert second.replayed == 1
    assert second.errors == ()
    assert db_session.query(FieldTechLocationPing).count() == 1


def test_unique_key_race_retries_the_complete_owner_boundary_once(
    db_session, monkeypatch
):
    user = _user(db_session)
    _profile(db_session, user)
    observation = _observation()
    db_session.commit()
    _update_collection(db_session, user, enabled=True, status="on_shift")

    real_execute = location_tracking_service.execute_owner_command
    attempts = 0

    def execute_once_raced(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise IntegrityError("insert", {}, Exception("duplicate identity"))
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(
        location_tracking_service,
        "execute_owner_command",
        execute_once_raced,
    )

    result = field_location_tracking.record_batch(
        db_session,
        RecordLocationBatchCommand(
            context=_context("test_location_race_retry"),
            principal=LocationPrincipal.from_auth(_auth(user)),
            purpose=FIELD_OPERATIONS_COLLECTION_PURPOSE,
            policy=_policy(),
            observations=(observation,),
        ),
    )

    assert attempts == 2
    assert result.accepted == 1
    assert result.replayed == 0
    assert db_session.query(FieldTechLocationPing).count() == 1


def test_reused_location_observation_identity_with_changed_evidence_conflicts(
    db_session,
):
    user = _user(db_session)
    _profile(db_session, user)
    observation_id = uuid4()
    captured_at = datetime.now(UTC)
    db_session.commit()

    _record_batch(
        db_session,
        user,
        [
            _observation(
                client_observation_id=observation_id,
                latitude=9.071,
                captured_at=captured_at,
            )
        ],
    )
    result = _record_batch(
        db_session,
        user,
        [
            _observation(
                client_observation_id=observation_id,
                latitude=9.072,
                captured_at=captured_at,
            )
        ],
    )

    assert result.accepted == 0
    assert result.replayed == 0
    assert result.errors[0].index == 0
    assert result.errors[0].client_observation_id == observation_id
    assert result.errors[0].code == "position_observation_identity_collision"
    assert result.errors[0].detail == (
        "Location observation identity was reused with different evidence."
    )
    assert db_session.query(FieldTechLocationPing).count() == 1


def test_future_location_evidence_is_rejected_without_mutation(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    db_session.commit()

    result = _record_batch(
        db_session,
        user,
        [_observation(captured_at=datetime.now(UTC) + timedelta(minutes=10))],
    )

    assert result.accepted == 0
    assert result.errors[0].code == "position_observation_future_timestamp"
    assert db_session.query(FieldTechLocationPing).count() == 0


def test_location_owner_rejects_an_active_caller_transaction(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    db_session.commit()
    _update_collection(db_session, user, enabled=True, status="on_shift")
    db_session.get(SystemUser, user.id)

    with pytest.raises(DomainError) as exc:
        field_location_tracking.record_batch(
            db_session,
            RecordLocationBatchCommand(
                context=_context("test_active_caller_transaction"),
                principal=LocationPrincipal.from_auth(_auth(user)),
                purpose=FIELD_OPERATIONS_COLLECTION_PURPOSE,
                policy=_policy(),
                observations=(_observation(),),
            ),
        )

    assert (
        exc.value.code == "operations.position_observations.active_caller_transaction"
    )
    assert db_session.query(FieldTechLocationPing).count() == 0


def test_location_owner_requires_active_collection_grant(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    db_session.commit()

    with pytest.raises(DomainError) as exc:
        field_location_tracking.record_batch(
            db_session,
            RecordLocationBatchCommand(
                context=_context("test_missing_collection_grant"),
                principal=LocationPrincipal.from_auth(_auth(user)),
                purpose=FIELD_OPERATIONS_COLLECTION_PURPOSE,
                policy=_policy(),
                observations=(_observation(),),
            ),
        )

    assert exc.value.code == "position_observation_collection_not_granted"
    assert db_session.query(FieldTechLocationPing).count() == 0


def test_location_api_uses_canonical_work_order_id(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    db_session.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    sharing = client.put(
        "/api/v1/field/locations/sharing",
        json={"enabled": True, "status": "on_shift"},
    )
    assert sharing.status_code == 200

    response = client.post(
        "/api/v1/field/locations",
        json={
            "pings": [
                {
                    "client_observation_id": str(uuid4()),
                    "latitude": 9.071,
                    "longitude": 7.451,
                    "accuracy_m": 8,
                    "captured_at": datetime.now(UTC).isoformat(),
                    "work_order_id": "wo-live",
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert response.json()["replayed"] == 0
    ping = db_session.query(FieldTechLocationPing).one()
    assert ping.work_order_id == "wo-live"


def test_location_ping_cannot_mutate_product_presence_status(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    db_session.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    sharing = client.put(
        "/api/v1/field/locations/sharing",
        json={"enabled": True, "status": "on_shift"},
    )
    assert sharing.status_code == 200

    response = client.post(
        "/api/v1/field/locations",
        json={
            "pings": [
                {
                    "client_observation_id": str(uuid4()),
                    "latitude": 9.071,
                    "longitude": 7.451,
                    "accuracy_m": 8,
                    "captured_at": datetime.now(UTC).isoformat(),
                    "status": "busy",
                }
            ]
        },
    )

    assert response.status_code == 422
    assert db_session.query(FieldTechLocationPing).count() == 0
    assert (
        field_presence.get_status(
            db_session,
            LocationPrincipal.from_auth(_auth(user)),
        ).status
        == "on_shift"
    )


def test_position_ingest_never_executes_work_order_consequences_by_default(
    db_session,
):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    row = _work_order(db_session, subscriber)
    db_session.commit()

    result = _record_batch(
        db_session,
        user,
        [_observation(latitude=9.0711, longitude=7.4511)],
    )

    db_session.refresh(row)
    assert result.transitions == ()
    assert row.status == "dispatched"
    assert db_session.query(FieldJobEvent).count() == 0


def test_committed_position_event_drives_geofence_policy_once(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    row = _work_order(db_session, subscriber, crm_work_order_id="wo-geofence-on")
    _field_setting(db_session, "geofence_auto_status_enabled", "true")
    observation = _observation(latitude=9.0711, longitude=7.4511)
    db_session.commit()

    result = _record_batch(db_session, user, [observation])

    db_session.refresh(row)
    assert result.transitions == ()
    assert row.status == "in_progress"
    event = db_session.query(FieldJobEvent).one()
    assert event.event == "start"
    assert event.payload["source"] == "geofence"

    db_session.rollback()
    replay = _record_batch(db_session, user, [observation])
    assert replay.replayed == 1
    assert db_session.query(FieldJobEvent).count() == 1


def test_update_collection_uses_canonical_on_break_status(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    db_session.commit()

    tracking = _update_collection(
        db_session,
        user,
        enabled=True,
        status="on_break",
    )
    assert tracking.location_sharing_enabled is True
    assert (
        field_presence.get_status(
            db_session,
            LocationPrincipal.from_auth(_auth(user)),
        ).status
        == "on_break"
    )

    db_session.rollback()
    tracking = _update_collection(
        db_session,
        user,
        enabled=False,
        status="off_shift",
    )
    assert tracking.location_sharing_enabled is False
    assert (
        field_presence.get_status(
            db_session,
            LocationPrincipal.from_auth(_auth(user)),
        ).status
        == "off_shift"
    )


def test_unknown_status_is_rejected_without_grant_mutation(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    db_session.commit()

    with pytest.raises(DomainError) as exc:
        _update_collection(
            db_session,
            user,
            enabled=True,
            status="teleporting",
        )

    assert exc.value.code == "field_presence_invalid_status"
    tracking = field_location_tracking.get_tracking(
        db_session,
        LocationPrincipal.from_auth(_auth(user)),
    )
    assert tracking.location_sharing_enabled is False
    assert (
        field_presence.get_status(
            db_session,
            LocationPrincipal.from_auth(_auth(user)),
        ).status
        == "off_shift"
    )


def test_location_api_routes(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    db_session.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    sharing = client.put(
        "/api/v1/field/locations/sharing",
        json={"enabled": True, "status": "on_shift"},
    )
    assert sharing.status_code == 200
    assert sharing.json()["status"] == "on_shift"

    ingest = client.post(
        "/api/v1/field/locations",
        json={
            "pings": [
                {
                    "client_observation_id": str(uuid4()),
                    "latitude": 9.071,
                    "longitude": 7.451,
                    "accuracy_m": 8,
                    "captured_at": datetime.now(UTC).isoformat(),
                }
            ]
        },
    )
    assert ingest.status_code == 200
    assert ingest.json()["accepted"] == 1

    presence = client.get("/api/v1/field/locations/me")
    assert presence.status_code == 200
    assert presence.json()["last_latitude"] == 9.071
