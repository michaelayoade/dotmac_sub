"""NAS connection rules service."""

import logging
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.catalog import ConnectionType, NasConnectionRule
from app.services.common import coerce_uuid
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


class NasConnectionRules(ListResponseMixin):
    """Service class for per-device connection rules."""

    @staticmethod
    def get(db: Session, rule_id: str | UUID) -> NasConnectionRule:
        rule_id = coerce_uuid(rule_id)
        rule = db.get(NasConnectionRule, rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail="Connection rule not found")
        return cast(NasConnectionRule, rule)

    @staticmethod
    def list(
        db: Session,
        *,
        nas_device_id: str | UUID,
        is_active: bool | None = None,
    ) -> list[NasConnectionRule]:
        device_id = coerce_uuid(nas_device_id)
        query = select(NasConnectionRule).where(
            NasConnectionRule.nas_device_id == device_id
        )
        if is_active is not None:
            query = query.where(NasConnectionRule.is_active == is_active)
        query = query.order_by(
            NasConnectionRule.priority.asc(), NasConnectionRule.name.asc()
        )
        return list(db.execute(query).scalars().all())

    @staticmethod
    def create(
        db: Session,
        *,
        nas_device_id: str | UUID,
        name: str,
        connection_type: ConnectionType | str | None = None,
        ip_assignment_mode: str | None = None,
        rate_limit_profile: str | None = None,
        match_expression: str | None = None,
        priority: int = 100,
        is_active: bool = True,
        notes: str | None = None,
    ) -> NasConnectionRule:
        from app.services.nas.devices import NasDevices

        device = NasDevices.get(db, nas_device_id)
        rule_name = (name or "").strip()
        if not rule_name:
            raise HTTPException(status_code=400, detail="Rule name is required")

        normalized_connection_type = None
        if connection_type:
            normalized_connection_type = (
                connection_type
                if isinstance(connection_type, ConnectionType)
                else ConnectionType(connection_type)
            )

        rule = NasConnectionRule(
            nas_device_id=device.id,
            name=rule_name,
            connection_type=normalized_connection_type,
            ip_assignment_mode=(ip_assignment_mode or "").strip() or None,
            rate_limit_profile=(rate_limit_profile or "").strip() or None,
            match_expression=(match_expression or "").strip() or None,
            priority=priority,
            is_active=is_active,
            notes=(notes or "").strip() or None,
        )
        db.add(rule)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=400,
                detail="A connection rule with this name already exists for the selected device.",
            ) from exc
        db.refresh(rule)
        return rule

    @staticmethod
    def set_active(
        db: Session,
        *,
        rule_id: str | UUID,
        nas_device_id: str | UUID,
        is_active: bool,
    ) -> NasConnectionRule:
        rule = NasConnectionRules.get(db, rule_id)
        device_id = coerce_uuid(nas_device_id)
        if rule.nas_device_id != device_id:
            raise HTTPException(
                status_code=404, detail="Connection rule not found for NAS device"
            )
        rule.is_active = is_active
        db.commit()
        db.refresh(rule)
        return rule

    @staticmethod
    def delete(db: Session, *, rule_id: str | UUID, nas_device_id: str | UUID) -> None:
        rule = NasConnectionRules.get(db, rule_id)
        device_id = coerce_uuid(nas_device_id)
        if rule.nas_device_id != device_id:
            raise HTTPException(
                status_code=404, detail="Connection rule not found for NAS device"
            )
        db.delete(rule)
        db.commit()


def create_connection_rule_for_device(
    db: Session,
    *,
    device_id: str,
    name: str,
    connection_type: str | None,
    ip_assignment_mode: str | None,
    rate_limit_profile: str | None,
    match_expression: str | None,
    priority: int,
    notes: str | None,
) -> str:
    NasConnectionRules.create(
        db,
        nas_device_id=device_id,
        name=name,
        connection_type=connection_type or None,
        ip_assignment_mode=ip_assignment_mode,
        rate_limit_profile=rate_limit_profile,
        match_expression=match_expression,
        priority=priority,
        notes=notes,
    )
    return "Connection rule created."


def toggle_connection_rule_for_device(
    db: Session,
    *,
    device_id: str,
    rule_id: str,
    is_active_raw: str,
) -> str:
    active = is_active_raw.strip().lower() in {"1", "true", "yes", "on"}
    NasConnectionRules.set_active(
        db,
        rule_id=rule_id,
        nas_device_id=device_id,
        is_active=active,
    )
    return "Connection rule enabled." if active else "Connection rule disabled."


def delete_connection_rule_for_device(
    db: Session,
    *,
    device_id: str,
    rule_id: str,
) -> str:
    NasConnectionRules.delete(db, rule_id=rule_id, nas_device_id=device_id)
    return "Connection rule deleted."
