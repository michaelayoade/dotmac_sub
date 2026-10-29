"""Typed references and bounded queries over native infrastructure inventory."""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.infrastructure import InfrastructureType


class InfrastructureReference(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)
    type: InfrastructureType
    id: UUID


class InfrastructureSearch(BaseModel):
    model_config = ConfigDict(frozen=True)
    type: InfrastructureType
    query: str = Field(default="", max_length=120)
    limit: int = Field(default=20, ge=1, le=20)


class InfrastructureOption(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    label: str
    context: str | None = None


class InfrastructureOptions(BaseModel):
    model_config = ConfigDict(frozen=True)
    results: tuple[InfrastructureOption, ...]


class TicketLocationSource(StrEnum):
    base_station = "base_station"
    wireless_access_point = "wireless_access_point"
    fiber_access_point = "fiber_access_point"


class TicketLocationReference(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: TicketLocationSource
    id: UUID


class TicketLocationSuggestionQuery(BaseModel):
    model_config = ConfigDict(frozen=True)
    query: str = Field(default="", max_length=120)
    limit_per_source: int = Field(default=12, ge=1, le=20)


class TicketLocationSuggestion(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    source: TicketLocationSource
    label: str
    context: str | None = None


class TicketLocationSuggestions(BaseModel):
    model_config = ConfigDict(frozen=True)
    base_stations: tuple[TicketLocationSuggestion, ...]
    access_points: tuple[TicketLocationSuggestion, ...]


class ProjectInfrastructureChanged(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: Literal["project.infrastructure_changed"] = "project.infrastructure_changed"
    project_id: UUID
    command_id: UUID
    previous: InfrastructureReference | None
    current: InfrastructureReference | None
