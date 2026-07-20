"""CRUD manager for ONT assignments."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntAssignment
from app.schemas.network import OntAssignmentCreate, OntAssignmentUpdate
from app.services.crud import CRUDManager
from app.services.network._common import (
    SubscriberValidator,
    _apply_ordering,
    _apply_pagination,
)
from app.services.network.ont_assignment_commands import (
    OntAssignmentCommandError,
    OntAssignmentCommands,
)
from app.services.query_builders import apply_active_state, apply_optional_equals


class OntAssignments(CRUDManager[OntAssignment]):
    model = OntAssignment
    not_found_detail = "ONT assignment not found"

    def __init__(
        self,
        subscriber_validator: SubscriberValidator | None = None,
        command_owner: OntAssignmentCommands | None = None,
    ) -> None:
        self._subscriber_validator = subscriber_validator
        self._command_owner = command_owner or OntAssignmentCommands(
            subscriber_validator=subscriber_validator
        )

    def create(self, db: Session, payload: OntAssignmentCreate) -> OntAssignment:  # type: ignore[override]
        if not payload.active:
            raise HTTPException(
                status_code=410,
                detail="Historical ONT assignment creation is retired",
            )
        if payload.subscription_id is None or payload.pon_port_id is None:
            raise HTTPException(
                status_code=422,
                detail="Exact subscription_id and pon_port_id are required",
            )
        try:
            with db.begin_nested():
                result = self._command_owner.assign(
                    db,
                    ont_unit_id=payload.ont_unit_id,
                    subscription_id=payload.subscription_id,
                    pon_port_id=payload.pon_port_id,
                    subscriber_id=payload.subscriber_id,
                    service_address_id=payload.service_address_id,
                    notes=payload.notes,
                    source="legacy_crud_adapter",
                    commit=False,
                )

                assignment = result.assignment
                identity_fields = {
                    "ont_unit_id",
                    "pon_port_id",
                    "subscriber_id",
                    "subscription_id",
                    "service_address_id",
                    "assigned_at",
                    "active",
                    "notes",
                }
                for key, value in payload.model_dump().items():
                    if key not in identity_fields:
                        setattr(assignment, key, value)
        except OntAssignmentCommandError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        db.commit()
        db.refresh(assignment)
        return assignment

    @staticmethod
    def list(
        db: Session,
        pon_port_id: str | None = None,
        subscriber_id: str | None = None,
        subscription_id: str | None = None,
        active: bool | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
        ont_unit_id: str | None = None,
    ) -> list[OntAssignment]:
        stmt = select(OntAssignment)
        stmt = apply_optional_equals(
            stmt,
            {
                OntAssignment.ont_unit_id: ont_unit_id,
                OntAssignment.pon_port_id: pon_port_id,
                OntAssignment.subscriber_id: subscriber_id,
                OntAssignment.subscription_id: subscription_id,
            },
        )
        stmt = apply_active_state(stmt, OntAssignment.active, active)
        stmt = _apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": OntAssignment.created_at, "active": OntAssignment.active},
        )
        return list(db.scalars(_apply_pagination(stmt, limit, offset)).all())

    @classmethod
    def get(cls, db: Session, assignment_id: str) -> OntAssignment:
        return super().get(db, assignment_id)

    def update(  # type: ignore[override]
        self, db: Session, assignment_id: str, payload: OntAssignmentUpdate
    ) -> OntAssignment:
        assignment = self.get(db, assignment_id)
        data = payload.model_dump(exclude_unset=True)
        fields_set = set(payload.model_fields_set)
        identity_fields = {
            "active",
            "assigned_at",
            "ont_unit_id",
            "pon_port_id",
            "service_address_id",
            "subscriber_id",
            "subscription_id",
        }
        attempted_identity = fields_set.intersection(identity_fields)
        if attempted_identity:
            raise HTTPException(
                status_code=410,
                detail=(
                    "Direct ONT assignment identity updates are retired; use the "
                    "normal assignment command, explicit release, PON move, or "
                    "reviewed identity repair"
                ),
            )
        for key, value in data.items():
            setattr(assignment, key, value)
        db.commit()
        db.refresh(assignment)
        return assignment

    @classmethod
    def delete(cls, db: Session, assignment_id: str) -> None:  # type: ignore[override]
        del db, assignment_id
        raise HTTPException(
            status_code=410,
            detail="Direct ONT assignment deletion is retired; release it explicitly",
        )
