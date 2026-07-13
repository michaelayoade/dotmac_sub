"""CRUD manager for ONT assignments."""

from __future__ import annotations

from time import sleep

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.network import OntAssignment, OntUnit, PonPort
from app.schemas.network import OntAssignmentCreate, OntAssignmentUpdate
from app.services.crud import CRUDManager
from app.services.network._common import (
    SubscriberValidator,
    _apply_ordering,
    _apply_pagination,
)
from app.services.query_builders import apply_active_state, apply_optional_equals


def _validate_assignment_target(
    db: Session,
    *,
    ont_unit_id: object,
    pon_port_id: object | None,
    active: bool,
    current_assignment_id: object | None = None,
) -> tuple[OntUnit, PonPort | None]:
    ont = db.scalar(select(OntUnit).where(OntUnit.id == ont_unit_id).with_for_update())
    if not ont:
        raise HTTPException(status_code=404, detail="ONT unit not found")

    pon_port: PonPort | None = None
    if pon_port_id is not None:
        pon_port = db.scalar(
            select(PonPort).where(PonPort.id == pon_port_id).with_for_update()
        )
        if not pon_port or not bool(getattr(pon_port, "is_active", True)):
            raise HTTPException(status_code=404, detail="PON port not found")
        if ont.olt_device_id and pon_port.olt_id != ont.olt_device_id:
            raise HTTPException(
                status_code=400,
                detail="PON port does not belong to the ONT's OLT",
            )
        if active and pon_port.max_ont_capacity is not None:
            assigned_count = (
                db.scalar(
                    select(func.count(OntAssignment.id))
                    .where(OntAssignment.pon_port_id == pon_port.id)
                    .where(OntAssignment.active.is_(True))
                )
                or 0
            )
            if assigned_count >= pon_port.max_ont_capacity:
                raise HTTPException(
                    status_code=409,
                    detail="PON port ONT capacity has been reached",
                )

    if active:
        stmt = (
            select(OntAssignment)
            .where(OntAssignment.ont_unit_id == ont.id)
            .where(OntAssignment.active.is_(True))
            .with_for_update()
            .limit(1)
        )
        if current_assignment_id is not None:
            stmt = stmt.where(OntAssignment.id != current_assignment_id)
        existing_active = db.scalars(stmt).first()
        if existing_active is not None:
            raise HTTPException(
                status_code=409,
                detail="ONT already has an active assignment",
            )

    return ont, pon_port


def _raise_assignment_conflict(exc: IntegrityError) -> None:
    message = str(getattr(exc, "orig", exc))
    if "ix_ont_assignments_active_unit" in message or "ont_assignments" in message:
        raise HTTPException(
            status_code=409,
            detail="ONT already has an active assignment",
        ) from exc
    raise exc


def _is_retryable_assignment_error(exc: OperationalError) -> bool:
    message = str(getattr(exc, "orig", exc)).lower()
    return any(
        token in message
        for token in (
            "deadlock detected",
            "could not serialize access",
            "lock timeout",
        )
    )


def _validate_assignment_customer_links(
    db: Session,
    *,
    subscriber_id: object | None,
    service_address_id: object | None,
    subscriber_validator: SubscriberValidator | None,
) -> None:
    """Validate subscriber/service-address links via an injected validator.

    When ``subscriber_validator`` is ``None`` the network service is running
    in standalone mode: we still reject the obviously-inconsistent case of a
    service address without a subscriber, but defer to the validator for any
    subscriber-existence or address-ownership checks.
    """
    if subscriber_validator is None:
        if subscriber_id is None and service_address_id is not None:
            raise HTTPException(
                status_code=400,
                detail="Service address requires a subscriber",
            )
        return
    subscriber_validator.validate_assignment_customer_links(
        db,
        subscriber_id=subscriber_id,
        service_address_id=service_address_id,
    )


def _resolve_assignment_subscription(
    db: Session,
    *,
    subscription_id: object | None,
    subscriber_id: object | None,
    subscriber_validator: SubscriberValidator | None,
) -> tuple[object | None, object | None]:
    if subscription_id is None:
        return None, subscriber_id
    if subscriber_validator is None:
        raise HTTPException(
            status_code=503,
            detail="Subscription validation is unavailable",
        )
    return subscriber_validator.resolve_assignment_subscription(
        db,
        subscription_id=subscription_id,
        subscriber_id=subscriber_id,
    )


def _has_other_active_assignment(
    db: Session,
    *,
    ont_unit_id: object,
    exclude_assignment_id: object | None = None,
) -> bool:
    stmt = (
        select(OntAssignment.id)
        .where(OntAssignment.ont_unit_id == ont_unit_id)
        .where(OntAssignment.active.is_(True))
        .limit(1)
    )
    if exclude_assignment_id is not None:
        stmt = stmt.where(OntAssignment.id != exclude_assignment_id)
    return db.scalars(stmt).first() is not None


def _sync_ont_assignment_runtime(db: Session, ont: OntUnit) -> None:
    from app.services.network.cpe import ensure_cpe_for_ont

    has_active_assignment = _has_other_active_assignment(db, ont_unit_id=ont.id)
    ont.is_active = has_active_assignment
    ensure_cpe_for_ont(db, ont, commit=False, strict_existing_match=False)


class OntAssignments(CRUDManager[OntAssignment]):
    model = OntAssignment
    not_found_detail = "ONT assignment not found"

    def __init__(self, subscriber_validator: SubscriberValidator | None = None) -> None:
        self._subscriber_validator = subscriber_validator

    def create(self, db: Session, payload: OntAssignmentCreate) -> OntAssignment:  # type: ignore[override]
        data = payload.model_dump()
        subscription_id, subscriber_id = _resolve_assignment_subscription(
            db,
            subscription_id=data.get("subscription_id"),
            subscriber_id=data.get("subscriber_id"),
            subscriber_validator=self._subscriber_validator,
        )
        data["subscription_id"] = subscription_id
        data["subscriber_id"] = subscriber_id
        ont, _pon_port = _validate_assignment_target(
            db,
            ont_unit_id=payload.ont_unit_id,
            pon_port_id=payload.pon_port_id,
            active=payload.active,
        )
        _validate_assignment_customer_links(
            db,
            subscriber_id=subscriber_id,
            service_address_id=payload.service_address_id,
            subscriber_validator=self._subscriber_validator,
        )
        assignment = OntAssignment(**data)
        from app.services.network.cpe import ensure_cpe_for_ont

        for attempt in range(3):
            try:
                with db.begin_nested():
                    db.add(assignment)
                    db.flush()
                    if assignment.subscription_id is not None:
                        if self._subscriber_validator is None:
                            raise HTTPException(
                                status_code=503,
                                detail="Subscription device-intent bridge is unavailable",
                            )
                        self._subscriber_validator.apply_subscription_device_intent(
                            db,
                            subscription_id=assignment.subscription_id,
                            ont=ont,
                        )
                    if assignment.active:
                        ont.is_active = True
                        ensure_cpe_for_ont(db, ont, assignment, commit=False)
                    else:
                        _sync_ont_assignment_runtime(db, ont)
                db.commit()
                break
            except IntegrityError as exc:
                db.rollback()
                _raise_assignment_conflict(exc)
            except OperationalError as exc:
                db.rollback()
                if attempt >= 2 or not _is_retryable_assignment_error(exc):
                    raise
                sleep(0.05 * (2**attempt))
            except Exception:
                db.expire(ont)
                raise
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
        original_ont_unit_id = assignment.ont_unit_id
        data = payload.model_dump(exclude_unset=True)
        fields_set = set(payload.model_fields_set)
        target_ont_unit_id = data.get("ont_unit_id", assignment.ont_unit_id)
        target_pon_port_id = data.get("pon_port_id", assignment.pon_port_id)
        target_active = data.get("active", assignment.active)
        target_subscriber_id = (
            data.get("subscriber_id")
            if "subscriber_id" in fields_set
            else assignment.subscriber_id
        )
        target_subscription_id = (
            data.get("subscription_id")
            if "subscription_id" in fields_set
            else assignment.subscription_id
        )
        target_subscription_id, target_subscriber_id = _resolve_assignment_subscription(
            db,
            subscription_id=target_subscription_id,
            subscriber_id=target_subscriber_id,
            subscriber_validator=self._subscriber_validator,
        )
        if "subscription_id" in fields_set:
            data["subscription_id"] = target_subscription_id
        if target_subscription_id is not None:
            data["subscriber_id"] = target_subscriber_id
        target_service_address_id = (
            data.get("service_address_id")
            if "service_address_id" in fields_set
            else assignment.service_address_id
        )

        ont, _pon_port = _validate_assignment_target(
            db,
            ont_unit_id=target_ont_unit_id,
            pon_port_id=target_pon_port_id,
            active=bool(target_active),
            current_assignment_id=assignment.id,
        )
        _validate_assignment_customer_links(
            db,
            subscriber_id=target_subscriber_id,
            service_address_id=target_service_address_id,
            subscriber_validator=self._subscriber_validator,
        )

        original_ont = (
            ont
            if original_ont_unit_id == ont.id
            else db.get(OntUnit, original_ont_unit_id)
        )

        for attempt in range(3):
            try:
                with db.begin_nested():
                    for key, value in data.items():
                        setattr(assignment, key, value)
                    db.flush()

                    if assignment.subscription_id is not None:
                        if self._subscriber_validator is None:
                            raise HTTPException(
                                status_code=503,
                                detail="Subscription device-intent bridge is unavailable",
                            )
                        self._subscriber_validator.apply_subscription_device_intent(
                            db,
                            subscription_id=assignment.subscription_id,
                            ont=ont,
                        )

                    if assignment.active:
                        ont.is_active = True
                    else:
                        _sync_ont_assignment_runtime(db, ont)

                    if original_ont is not None and original_ont.id != ont.id:
                        _sync_ont_assignment_runtime(db, original_ont)

                    if assignment.active:
                        from app.services.network.cpe import ensure_cpe_for_ont

                        ensure_cpe_for_ont(db, ont, assignment, commit=False)
                db.commit()
                break
            except IntegrityError as exc:
                db.rollback()
                _raise_assignment_conflict(exc)
            except OperationalError as exc:
                db.rollback()
                if attempt >= 2 or not _is_retryable_assignment_error(exc):
                    raise
                sleep(0.05 * (2**attempt))
            except Exception:
                if original_ont is not None:
                    db.expire(original_ont)
                if ont is not original_ont:
                    db.expire(ont)
                raise
        db.refresh(assignment)
        return assignment

    @classmethod
    def delete(cls, db: Session, assignment_id: str) -> None:  # type: ignore[override]
        assignment = cls.get(db, assignment_id)
        ont = db.get(OntUnit, assignment.ont_unit_id)
        try:
            with db.begin_nested():
                db.delete(assignment)
                db.flush()
                if ont is not None:
                    _sync_ont_assignment_runtime(db, ont)
            db.commit()
        except Exception:
            if ont is not None:
                db.expire(ont)
            raise
