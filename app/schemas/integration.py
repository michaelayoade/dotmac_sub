from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.integration import (
    IntegrationJobType,
    IntegrationRunStatus,
    IntegrationScheduleType,
    IntegrationTargetType,
)
from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationInstallationState,
    IntegrationValidationStatus,
)


class IntegrationTargetBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    target_type: IntegrationTargetType = IntegrationTargetType.custom
    is_active: bool = True
    notes: str | None = None


class IntegrationTargetCreate(IntegrationTargetBase):
    pass


class IntegrationTargetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    target_type: IntegrationTargetType | None = None
    is_active: bool | None = None
    notes: str | None = None


class IntegrationTargetRead(IntegrationTargetBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class IntegrationJobBase(BaseModel):
    target_id: UUID
    capability_binding_id: UUID | None = None
    name: str = Field(min_length=1, max_length=160)
    job_type: IntegrationJobType = IntegrationJobType.sync
    schedule_type: IntegrationScheduleType = IntegrationScheduleType.manual
    interval_minutes: int | None = None
    interval_seconds: int | None = None
    entity_type: str | None = None
    direction: str | None = None
    trigger_mode: str | None = None
    mapping_config: dict | None = None
    filter_config: dict | None = None
    conflict_policy: str | None = None
    is_active: bool = True
    last_run_at: datetime | None = None
    notes: str | None = None


class IntegrationJobCreate(IntegrationJobBase):
    capability_binding_id: UUID


class IntegrationJobUpdate(BaseModel):
    target_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    job_type: IntegrationJobType | None = None
    schedule_type: IntegrationScheduleType | None = None
    interval_minutes: int | None = None
    interval_seconds: int | None = None
    capability_binding_id: UUID | None = None
    entity_type: str | None = None
    direction: str | None = None
    trigger_mode: str | None = None
    mapping_config: dict | None = None
    filter_config: dict | None = None
    conflict_policy: str | None = None
    is_active: bool | None = None
    last_run_at: datetime | None = None
    notes: str | None = None


class IntegrationJobRead(IntegrationJobBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class IntegrationRunBase(BaseModel):
    job_id: UUID
    status: IntegrationRunStatus = IntegrationRunStatus.running
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    metrics: dict | None = None


class IntegrationRunRead(IntegrationRunBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class IntegrationInstallationCreate(BaseModel):
    connector_key: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    environment: str = Field(
        default="production", pattern="^(production|sandbox|test)$"
    )


class IntegrationConfigRevisionCreate(BaseModel):
    config: dict = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    schema_version: str = Field(default="v1", min_length=1, max_length=32)


class IntegrationCapabilityBindingUpsert(BaseModel):
    scope: dict = Field(default_factory=dict)
    policy: dict = Field(default_factory=dict)


class IntegrationLifecycleCommand(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class IntegrationManifestPin(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    connector_version: str = Field(min_length=1, max_length=32)
    manifest_digest: str = Field(pattern="^[0-9a-f]{64}$")


class IntegrationManifestAdoptionPreviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    installation_id: UUID
    connector_key: str
    environment: str
    installation_state: str
    installed_pin: IntegrationManifestPin
    target_pin: IntegrationManifestPin | None
    pin_state: str
    adoption_required: bool
    ready: bool
    blocking_errors: tuple[str, ...]


class IntegrationManifestAdoptionCommand(BaseModel):
    expected_installed_pin: IntegrationManifestPin
    target_pin: IntegrationManifestPin
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=200)


class IntegrationManifestAdoptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    installation_id: UUID
    connector_key: str
    previous_pin: IntegrationManifestPin
    adopted_pin: IntegrationManifestPin
    installation_state: str
    replayed: bool


class IntegrationConfigRevisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    installation_id: UUID
    revision: int
    schema_version: str
    config_json: dict
    secret_refs: dict[str, str]
    config_digest: str
    validation_status: IntegrationValidationStatus
    validation_errors: list | None
    created_by: str | None
    created_at: datetime


class IntegrationCapabilityBindingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    installation_id: UUID
    capability_id: str
    state: IntegrationBindingState
    scope_json: dict
    policy_json: dict
    enabled_at: datetime | None
    disabled_at: datetime | None
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


class IntegrationInstallationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    connector_key: str
    connector_version: str
    manifest_digest: str
    name: str
    environment: str
    state: IntegrationInstallationState
    state_reason: str | None
    current_config_revision_id: UUID | None
    current_config_revision: IntegrationConfigRevisionRead | None
    capability_bindings: list[IntegrationCapabilityBindingRead]
    validated_at: datetime | None
    enabled_at: datetime | None
    disabled_at: datetime | None
    quarantined_at: datetime | None
    retired_at: datetime | None
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


class IntegrationEventSubscriptionCreate(BaseModel):
    capability_binding_id: UUID
    event_type: str = Field(min_length=1, max_length=160)
    filter_json: dict = Field(default_factory=dict)
    payload_policy_json: dict = Field(default_factory=dict)


class IntegrationEventSubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    capability_binding_id: UUID
    event_type: str
    state: str
    filter_json: dict
    payload_policy_json: dict
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


class IntegrationDeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subscription_id: UUID | None
    capability_binding_id: UUID
    source_event_id: str
    event_type: str
    destination_key: str
    idempotency_key: str
    payload_digest: str
    state: str
    attempt_count: int
    next_attempt_at: datetime | None
    last_attempt_at: datetime | None
    delivered_at: datetime | None
    response_status: int | None
    external_receipt_json: dict
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class IntegrationInboxRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    installation_id: UUID
    capability_binding_id: UUID
    provider_event_id: str
    event_type: str
    payload_digest: str
    payload_json: dict
    state: str
    attempt_count: int
    consequence_json: dict
    error_code: str | None
    error_detail: str | None
    received_at: datetime
    processed_at: datetime | None
    created_at: datetime
    updated_at: datetime
