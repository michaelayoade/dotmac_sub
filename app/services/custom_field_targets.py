"""Closed runtime adapters for custom-field target identity validation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.sales import Lead, Quote, SalesOrder
from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.services import custom_field_capabilities


class CustomFieldTargetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TargetRuntimeAdapter:
    key: str
    exists: Callable[[Session, UUID], bool]
    detail_identifier: Callable[[Session, UUID], str | None] | None = None


def _row_exists(db: Session, target_id: UUID, id_column: Any) -> bool:
    return db.scalar(select(id_column).where(id_column == target_id)) is not None


_ADAPTERS: tuple[TargetRuntimeAdapter, ...] = (
    TargetRuntimeAdapter(
        key="subscriber",
        exists=lambda db, target_id: _row_exists(db, target_id, Subscriber.id),
    ),
    TargetRuntimeAdapter(
        key="project",
        exists=lambda db, target_id: _row_exists(db, target_id, Project.id),
    ),
    TargetRuntimeAdapter(
        key="support_ticket",
        exists=lambda db, target_id: _row_exists(db, target_id, Ticket.id),
    ),
    TargetRuntimeAdapter(
        key="work_order",
        exists=lambda db, target_id: _row_exists(db, target_id, WorkOrder.id),
        detail_identifier=lambda db, target_id: db.scalar(
            select(WorkOrder.public_id).where(WorkOrder.id == target_id)
        ),
    ),
    TargetRuntimeAdapter(
        key="lead",
        exists=lambda db, target_id: _row_exists(db, target_id, Lead.id),
    ),
    TargetRuntimeAdapter(
        key="quote",
        exists=lambda db, target_id: _row_exists(db, target_id, Quote.id),
    ),
    TargetRuntimeAdapter(
        key="sales_order",
        exists=lambda db, target_id: _row_exists(db, target_id, SalesOrder.id),
    ),
)


def runtime_registry_errors() -> tuple[str, ...]:
    declared = {
        target.key
        for module in custom_field_capabilities.registered_module_manifests()
        for target in module.targets
    }
    adapter_keys = [adapter.key for adapter in _ADAPTERS]
    errors = [
        f"custom-field target {key!r} has no runtime adapter"
        for key in sorted(declared - set(adapter_keys))
    ]
    errors.extend(
        f"custom-field runtime adapter {key!r} has no target declaration"
        for key in sorted(set(adapter_keys) - declared)
    )
    errors.extend(
        f"custom-field runtime adapter {key!r} is duplicated"
        for key in sorted({key for key in adapter_keys if adapter_keys.count(key) > 1})
    )
    return tuple(errors)


def _adapter(target_type: str) -> TargetRuntimeAdapter:
    target = custom_field_capabilities.target_capability(target_type)
    matches = [adapter for adapter in _ADAPTERS if adapter.key == target.key]
    if len(matches) != 1:
        raise CustomFieldTargetError(
            f"Custom-field target {target_type!r} has no exact runtime adapter."
        )
    return matches[0]


def target_exists(db: Session, *, target_type: str, target_id: UUID) -> bool:
    return _adapter(target_type).exists(db, target_id)


def target_detail_path(db: Session, *, target_type: str, target_id: UUID) -> str:
    target = custom_field_capabilities.target_capability(target_type)
    adapter = _adapter(target.key)
    identifier = (
        adapter.detail_identifier(db, target_id)
        if adapter.detail_identifier is not None
        else str(target_id)
    )
    if not identifier:
        raise CustomFieldTargetError(
            f"Custom-field target {target_type!r} could not resolve its detail path."
        )
    return target.detail_path_template.format(target_id=identifier)


__all__ = [
    "CustomFieldTargetError",
    "runtime_registry_errors",
    "target_detail_path",
    "target_exists",
]
