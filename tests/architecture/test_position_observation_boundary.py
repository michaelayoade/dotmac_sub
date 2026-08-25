from __future__ import annotations

from pathlib import Path

from app.services.sot_manifest import OwnerRole, TransactionMode
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_position_observation_owner_is_fully_contracted() -> None:
    service = service_relationship("operations.position_observations")

    assert service.module == "app.services.field.location_tracking"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert {concern.name: concern.role for concern in service.contract.concerns} == {
        "field position observations": OwnerRole.OBSERVATION_COLLECTOR,
        "field technician current-position projection": OwnerRole.PROJECTION_WRITER,
        "field location collection grant": OwnerRole.AUTHORITATIVE_RECORD,
        "position observation retention": OwnerRole.COMMAND_WRITER,
    }


def test_position_observation_owner_is_transaction_neutral_below_boundary() -> None:
    source = _source("app/services/field/location_tracking.py")

    assert "db.commit(" not in source
    assert "db.rollback(" not in source
    assert "geofence" not in source
    assert "field_transitions" not in source
    assert "execute_owner_command(" in source


def test_position_observation_owner_has_no_workforce_status_or_fixed_purpose() -> None:
    source = _source("app/services/field/location_tracking.py")

    assert "FIELD_PRESENCE_STATUSES" not in source
    assert "COLLECTION_PURPOSE" not in source
    assert "presence.status =" not in source
    assert "status: str" not in source
    assert "status=presence.status" not in source
    assert "context_ref: str | None" in source
    assert "purpose: str" in source


def test_position_observation_validation_is_a_typed_product_policy() -> None:
    source = _source("app/services/field/location_tracking.py")
    api = _source("app/api/field/locations.py")
    policy = _source("app/services/field/location_policy.py")
    schemas = _source("app/schemas/field.py")

    assert "class PositionObservationPolicy:" in source
    assert "policy: PositionObservationPolicy" in source
    assert "MAX_BATCH_PINGS" not in source
    assert "MAX_FUTURE_SKEW" not in source
    assert "MAX_ACCURACY_M" not in source
    assert "policy=field_location_policy(db)" in api
    assert '"location_max_batch_size"' in policy
    assert '"location_max_future_skew_seconds"' in policy
    assert '"location_max_accuracy_meters"' in policy
    assert (
        "pings: list[LocationPingInput] = Field(min_length=1, max_length=200)"
        not in schemas
    )
    assert "accuracy_m: float = Field(ge=0, le=1000)" not in schemas


def test_sub_binds_work_context_and_status_outside_positioning_owner() -> None:
    api = _source("app/api/field/locations.py")
    presence = _source("app/services/field/presence.py")

    assert "context_ref=ping.work_order_id" in api
    assert "purpose=FIELD_OPERATIONS_COLLECTION_PURPOSE" in api
    assert "field_presence.update_status(" in api
    assert 'owner="operations.field_presence"' in presence
    assert "FIELD_PRESENCE_STATUSES" in presence
    assert "presence.status =" in presence


def test_position_observation_api_passes_typed_commands() -> None:
    source = _source("app/api/field/locations.py")

    assert "RecordLocationBatchCommand(" in source
    assert "PositionObservation(" in source
    assert "UpdateLocationCollectionCommand(" in source
    assert "model_dump(" not in source


def test_geofence_consequence_has_a_separate_product_policy_owner() -> None:
    service = service_relationship("operations.field_geofence_policy")
    handler = _source("app/services/events/handlers/field_geofence_policy.py")

    assert service.module == "app.services.field.geofence"
    assert service.depends_on == (
        "operations.position_observations",
        "operations.field_completion",
        "control.domain_settings",
        "events.store",
    )
    assert "position_observation_recorded" in handler
    assert "consume_position_observation" in handler


def test_mobile_assigns_identity_and_preserves_device_evidence() -> None:
    source = _source("field_mobile/lib/features/location/location_ping_service.dart")
    device_source = _source("field_mobile/lib/core/location/device_location.dart")

    assert "'client_observation_id': _observationId()" in source
    assert "'accuracy_m': point.accuracyM" in source
    assert "point.capturedAt ?? _clock()" in source
    assert "accuracyM: position.accuracy" in device_source
    assert "capturedAt: position.timestamp.toUtc()" in device_source
    assert "'status': _shift.apiValue" not in source


def test_sot_map_names_position_observation_owner() -> None:
    source = _source("docs/SOT_RELATIONSHIP_MAP.md")

    assert "`operations.position_observations`" in source
    assert "docs/designs/POSITION_OBSERVATION_SOT.md" in source


def test_positioning_migration_reconciles_the_current_squashed_baseline() -> None:
    source = _source("alembic/versions/542_position_observation_identity.py")

    assert "def _column_names(" in source
    assert 'if "crm_work_order_id" in ping_columns:' in source
    assert 'elif "work_order_id" not in ping_columns:' in source
    assert "found both legacy and canonical work-order columns" in source
    assert "not in _index_names(_PING_TABLE)" in source
