"""Typed customer service request choices owned by Sales self-service."""

from __future__ import annotations

from enum import StrEnum

from app.models.project import ProjectType


class ServiceRequestKind(StrEnum):
    installation = "installation"
    relocation = "relocation"


class ServiceRequestOption(StrEnum):
    fiber_installation = "fiber_installation"
    airfiber_installation = "airfiber_installation"
    fiber_to_fiber_relocation = "fiber_to_fiber_relocation"
    airfiber_to_fiber_relocation = "airfiber_to_fiber_relocation"
    fiber_to_airfiber_relocation = "fiber_to_airfiber_relocation"
    airfiber_to_airfiber_relocation_no_cable_replacement = (
        "airfiber_to_airfiber_relocation_no_cable_replacement"
    )
    airfiber_to_airfiber_relocation_with_cable_replacement = (
        "airfiber_to_airfiber_relocation_with_cable_replacement"
    )

    @property
    def kind(self) -> ServiceRequestKind:
        return (
            ServiceRequestKind.installation
            if self
            in {
                ServiceRequestOption.fiber_installation,
                ServiceRequestOption.airfiber_installation,
            }
            else ServiceRequestKind.relocation
        )

    @property
    def source_access_type(self) -> str | None:
        if self.kind is ServiceRequestKind.installation:
            return None
        if self in {
            ServiceRequestOption.fiber_to_fiber_relocation,
            ServiceRequestOption.fiber_to_airfiber_relocation,
        }:
            return "fiber"
        return "fixed_wireless"

    @property
    def destination_access_type(self) -> str:
        if self in {
            ServiceRequestOption.fiber_installation,
            ServiceRequestOption.fiber_to_fiber_relocation,
            ServiceRequestOption.airfiber_to_fiber_relocation,
        }:
            return "fiber"
        return "fixed_wireless"

    @property
    def project_type(self) -> ProjectType:
        if self.kind is ServiceRequestKind.installation:
            return (
                ProjectType.fiber_optics_installation
                if self.destination_access_type == "fiber"
                else ProjectType.air_fiber_installation
            )
        return (
            ProjectType.fiber_optics_relocation
            if self.destination_access_type == "fiber"
            else ProjectType.air_fiber_relocation
        )
