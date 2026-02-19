from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.provisioning import (
    AppointmentStatus,
    ProvisioningRunStatus,
    ProvisioningStepType,
    ProvisioningVendor,
    ServiceOrderStatus,
    ServiceOrderType,
    ServiceState,
    TaskStatus,
)

# Deleted model - commented out
# from app.models.projects import ProjectType
ProjectType = str  # Fallback type alias


class ServiceOrderBase(BaseModel):
    subscriber_id: UUID = Field(
        validation_alias="account_id", serialization_alias="account_id"
    )
    subscription_id: UUID | None = None
    requested_by_contact_id: UUID | None = None
    status: ServiceOrderStatus = ServiceOrderStatus.draft
    order_type: ServiceOrderType | None = None
    project_type: ProjectType | None = None
    notes: str | None = None


class ServiceOrderCreate(ServiceOrderBase):
    pass


class ServiceOrderUpdate(BaseModel):
    subscriber_id: UUID | None = Field(
        default=None, validation_alias="account_id", serialization_alias="account_id"
    )
    subscription_id: UUID | None = None
    requested_by_contact_id: UUID | None = None
    status: ServiceOrderStatus | None = None
    order_type: ServiceOrderType | None = None
    project_type: ProjectType | None = None
    notes: str | None = None


class ServiceOrderRead(ServiceOrderBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class InstallAppointmentBase(BaseModel):
    service_order_id: UUID
    scheduled_start: datetime
    scheduled_end: datetime
    technician: str | None = Field(default=None, max_length=120)
    status: AppointmentStatus = AppointmentStatus.proposed
    notes: str | None = None
    is_self_install: bool = False


class InstallAppointmentCreate(InstallAppointmentBase):
    pass


class InstallAppointmentUpdate(BaseModel):
    service_order_id: UUID | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    technician: str | None = Field(default=None, max_length=120)
    status: AppointmentStatus | None = None
    notes: str | None = None
    is_self_install: bool | None = None

    @model_validator(mode="after")
    def _validate_schedule_order(self) -> InstallAppointmentUpdate:
        if self.scheduled_start and self.scheduled_end:
            if self.scheduled_start >= self.scheduled_end:
                raise ValueError("scheduled_start must be before scheduled_end")
        return self


class InstallAppointmentRead(InstallAppointmentBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class ProvisioningTaskBase(BaseModel):
    service_order_id: UUID
    name: str = Field(min_length=1, max_length=160)
    status: TaskStatus = TaskStatus.pending
    assigned_to: str | None = Field(default=None, max_length=120)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    notes: str | None = None


class ProvisioningTaskCreate(ProvisioningTaskBase):
    pass


class ProvisioningTaskUpdate(BaseModel):
    service_order_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    status: TaskStatus | None = None
    assigned_to: str | None = Field(default=None, max_length=120)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    notes: str | None = None


class ProvisioningTaskRead(ProvisioningTaskBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class ServiceStateTransitionBase(BaseModel):
    service_order_id: UUID
    from_state: ServiceState | None = None
    to_state: ServiceState
    reason: str | None = Field(default=None, max_length=200)
    changed_by: str | None = Field(default=None, max_length=120)
    changed_at: datetime | None = None


class ServiceStateTransitionCreate(ServiceStateTransitionBase):
    pass


class ServiceStateTransitionUpdate(BaseModel):
    service_order_id: UUID | None = None
    from_state: ServiceState | None = None
    to_state: ServiceState | None = None
    reason: str | None = Field(default=None, max_length=200)
    changed_by: str | None = Field(default=None, max_length=120)
    changed_at: datetime | None = None


class ServiceStateTransitionRead(ServiceStateTransitionBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class ProvisioningWorkflowBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    vendor: ProvisioningVendor = ProvisioningVendor.other
    description: str | None = None
    is_active: bool = True


class ProvisioningWorkflowCreate(ProvisioningWorkflowBase):
    pass


class ProvisioningWorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    vendor: ProvisioningVendor | None = None
    description: str | None = None
    is_active: bool | None = None


class ProvisioningWorkflowRead(ProvisioningWorkflowBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class ProvisioningStepBase(BaseModel):
    workflow_id: UUID
    name: str = Field(min_length=1, max_length=160)
    step_type: ProvisioningStepType
    order_index: int = 0
    config: dict | None = None
    is_active: bool = True


class ProvisioningStepCreate(ProvisioningStepBase):
    pass


class ProvisioningStepUpdate(BaseModel):
    workflow_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    step_type: ProvisioningStepType | None = None
    order_index: int | None = None
    config: dict | None = None
    is_active: bool | None = None


class ProvisioningStepRead(ProvisioningStepBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class ProvisioningRunBase(BaseModel):
    workflow_id: UUID
    service_order_id: UUID | None = None
    subscription_id: UUID | None = None
    status: ProvisioningRunStatus = ProvisioningRunStatus.pending
    started_at: datetime | None = None
    completed_at: datetime | None = None
    input_payload: dict | None = None
    output_payload: dict | None = None
    error_message: str | None = None


class ProvisioningRunCreate(ProvisioningRunBase):
    pass


class ProvisioningRunUpdate(BaseModel):
    workflow_id: UUID | None = None
    service_order_id: UUID | None = None
    subscription_id: UUID | None = None
    status: ProvisioningRunStatus | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    input_payload: dict | None = None
    output_payload: dict | None = None
    error_message: str | None = None


class ProvisioningRunRead(ProvisioningRunBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class ProvisioningRunStart(BaseModel):
    service_order_id: UUID | None = None
    subscription_id: UUID | None = None
    input_payload: dict | None = None
