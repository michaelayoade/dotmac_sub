"""Report imported ONT type profile mapping coverage per OLT."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.network import (
    OLTDevice,
    OltOntRegistration,
    OltOnuTypeProfileMapping,
    OntUnit,
)
from app.services.network.equipment_identity import normalize_ont_equipment_id


@dataclass(frozen=True)
class MissingOltMapping:
    olt_id: str
    olt_name: str
    equipment_id: str
    inventory_count: int = 0
    imported_registration_count: int = 0

    @property
    def total_count(self) -> int:
        return self.inventory_count + self.imported_registration_count


@dataclass(frozen=True)
class OltMappingCoverage:
    olt_id: str
    olt_name: str
    mapped_equipment_count: int
    observed_equipment_count: int
    missing: list[MissingOltMapping] = field(default_factory=list)

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    @property
    def is_complete(self) -> bool:
        return self.missing_count == 0


def _equipment_id_from_ont(ont: OntUnit) -> str | None:
    equipment_id = normalize_ont_equipment_id(getattr(ont, "model", None))
    if equipment_id:
        return equipment_id
    onu_type = getattr(ont, "onu_type", None)
    return normalize_ont_equipment_id(getattr(onu_type, "name", None))


def _selected_olts(
    db: Session,
    *,
    olt_id: str | None = None,
    olt_name: str | None = None,
    active_only: bool = True,
) -> list[OLTDevice]:
    stmt = select(OLTDevice).order_by(OLTDevice.name)
    if olt_id:
        stmt = stmt.where(OLTDevice.id == olt_id)
    if olt_name:
        stmt = stmt.where(OLTDevice.name == olt_name)
    if active_only:
        stmt = stmt.where(OLTDevice.is_active.is_(True))
    return list(db.scalars(stmt).all())


def build_olt_mapping_coverage_report(
    db: Session,
    *,
    olt_id: str | None = None,
    olt_name: str | None = None,
    active_only: bool = True,
) -> list[OltMappingCoverage]:
    """Return missing ONT equipment-to-profile mappings by OLT.

    Observed equipment IDs come from two DB sources:
    - active ``OntUnit`` inventory rows attached to the OLT
    - active imported ``OltOntRegistration`` rows from the OLT state import
    """
    report: list[OltMappingCoverage] = []
    for olt in _selected_olts(
        db,
        olt_id=olt_id,
        olt_name=olt_name,
        active_only=active_only,
    ):
        inventory_counts: dict[str, int] = {}
        for ont in db.scalars(
            select(OntUnit)
            .options(selectinload(OntUnit.onu_type))
            .where(OntUnit.olt_device_id == olt.id)
            .where(OntUnit.is_active.is_(True))
        ):
            equipment_id = _equipment_id_from_ont(ont)
            if equipment_id:
                inventory_counts[equipment_id] = inventory_counts.get(equipment_id, 0) + 1

        registration_counts: dict[str, int] = {}
        for registration in db.scalars(
            select(OltOntRegistration)
            .where(OltOntRegistration.olt_id == olt.id)
            .where(OltOntRegistration.is_active.is_(True))
            .where(OltOntRegistration.equipment_id.isnot(None))
        ):
            equipment_id = normalize_ont_equipment_id(registration.equipment_id)
            if equipment_id:
                registration_counts[equipment_id] = (
                    registration_counts.get(equipment_id, 0) + 1
                )

        mapped = {
            equipment_id
            for mapping in db.scalars(
                select(OltOnuTypeProfileMapping).where(
                    OltOnuTypeProfileMapping.olt_id == olt.id
                )
            )
            if (equipment_id := normalize_ont_equipment_id(mapping.equipment_id))
        }
        observed = set(inventory_counts) | set(registration_counts)
        missing = [
            MissingOltMapping(
                olt_id=str(olt.id),
                olt_name=olt.name,
                equipment_id=equipment_id,
                inventory_count=inventory_counts.get(equipment_id, 0),
                imported_registration_count=registration_counts.get(equipment_id, 0),
            )
            for equipment_id in sorted(observed - mapped)
        ]
        report.append(
            OltMappingCoverage(
                olt_id=str(olt.id),
                olt_name=olt.name,
                mapped_equipment_count=len(mapped),
                observed_equipment_count=len(observed),
                missing=missing,
            )
        )
    return report
