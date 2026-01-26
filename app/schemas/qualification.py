from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.qualification import (
    BuildoutMilestoneStatus,
    BuildoutProjectStatus,
    BuildoutRequestStatus,
    BuildoutStatus,
    QualificationStatus,
)


class CoverageAreaBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str = Field(min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=80)
    zone_key: str | None = Field(default=None, max_length=80)
    buildout_status: BuildoutStatus = BuildoutStatus.planned
    buildout_window: str | None = Field(default=None, max_length=120)
    serviceable: bool = True
    priority: int = 0
    geometry_geojson: dict
    constraints: dict | None = None
    is_active: bool = True


class CoverageAreaCreate(CoverageAreaBase):
    pass


class CoverageAreaUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=80)
    zone_key: str | None = Field(default=None, max_length=80)
    buildout_status: BuildoutStatus | None = None
    buildout_window: str | None = Field(default=None, max_length=120)
    serviceable: bool | None = None
    priority: int | None = None
    geometry_geojson: dict | None = None
    constraints: dict | None = None
    is_active: bool | None = None


class CoverageAreaRead(CoverageAreaBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    min_latitude: float | None = None
    max_latitude: float | None = None
    min_longitude: float | None = None
    max_longitude: float | None = None
    created_at: datetime
    updated_at: datetime


class ServiceQualificationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    address_id: UUID | None = None
    latitude: float | None = None
    longitude: float | None = None
    requested_tech: str | None = Field(default=None, max_length=60)
    zone_key: str | None = Field(default=None, max_length=80)
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )


class ServiceQualificationBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    coverage_area_id: UUID | None = None
    address_id: UUID | None = None
    latitude: float
    longitude: float
    requested_tech: str | None = None
    status: QualificationStatus = QualificationStatus.ineligible
    buildout_status: BuildoutStatus | None = None
    estimated_install_window: str | None = None
    reasons: list | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )


class ServiceQualificationCreate(ServiceQualificationBase):
    pass


class ServiceQualificationRead(ServiceQualificationBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime


class BuildoutRequestBase(BaseModel):
    qualification_id: UUID | None = None
    coverage_area_id: UUID | None = None
    address_id: UUID | None = None
    requested_by: str | None = Field(default=None, max_length=120)
    status: BuildoutRequestStatus = BuildoutRequestStatus.submitted
    notes: str | None = None


class BuildoutRequestCreate(BuildoutRequestBase):
    pass


class BuildoutRequestUpdate(BaseModel):
    qualification_id: UUID | None = None
    coverage_area_id: UUID | None = None
    address_id: UUID | None = None
    requested_by: str | None = Field(default=None, max_length=120)
    status: BuildoutRequestStatus | None = None
    notes: str | None = None


class BuildoutRequestRead(BuildoutRequestBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class BuildoutProjectBase(BaseModel):
    request_id: UUID | None = None
    coverage_area_id: UUID | None = None
    address_id: UUID | None = None
    status: BuildoutProjectStatus = BuildoutProjectStatus.planned
    progress_percent: int = Field(default=0, ge=0, le=100)
    target_ready_date: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    notes: str | None = None


class BuildoutProjectCreate(BuildoutProjectBase):
    pass


class BuildoutProjectUpdate(BaseModel):
    request_id: UUID | None = None
    coverage_area_id: UUID | None = None
    address_id: UUID | None = None
    status: BuildoutProjectStatus | None = None
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    target_ready_date: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    notes: str | None = None


class BuildoutProjectRead(BuildoutProjectBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class BuildoutMilestoneBase(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=160)
    status: BuildoutMilestoneStatus = BuildoutMilestoneStatus.pending
    order_index: int = 0
    due_at: datetime | None = None
    completed_at: datetime | None = None
    notes: str | None = None


class BuildoutMilestoneCreate(BuildoutMilestoneBase):
    pass


class BuildoutMilestoneUpdate(BaseModel):
    project_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    status: BuildoutMilestoneStatus | None = None
    order_index: int | None = None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    notes: str | None = None


class BuildoutMilestoneRead(BuildoutMilestoneBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class BuildoutUpdateBase(BaseModel):
    project_id: UUID
    status: BuildoutProjectStatus = BuildoutProjectStatus.planned
    message: str | None = None


class BuildoutUpdateCreate(BuildoutUpdateBase):
    pass


class BuildoutUpdateRead(BuildoutUpdateBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class BuildoutApproveRequest(BaseModel):
    target_ready_date: datetime | None = None
    notes: str | None = None


class BuildoutUpdateListRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    status: BuildoutProjectStatus
    message: str | None
    created_at: datetime
