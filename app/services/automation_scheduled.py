"""Record providers for scheduled Automation Center evaluations.

The scheduler owns cadence and idempotency; this module only reads each
module's authoritative records and projects the declared condition fields into
the custom event envelope consumed by the normal automation runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.automation import (
    AutomationRule,
    AutomationRuleVersion,
    AutomationScheduledRun,
)
from app.models.project import Project
from app.models.sales import Lead, Quote, SalesOrder
from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.models.work_order import WorkOrder

MAX_TARGETS_PER_PROVIDER = 2000


def claim_slot(
    session: Session,
    rule: AutomationRule,
    version: AutomationRuleVersion,
    slot: str,
) -> bool:
    """Claim one scheduled rule slot without allowing duplicate emissions."""
    try:
        with session.begin_nested():
            session.add(
                AutomationScheduledRun(
                    tenant_id=rule.tenant_id,
                    rule_id=rule.id,
                    rule_version_id=version.id,
                    slot_key=slot,
                )
            )
            session.flush()
    except IntegrityError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class ScheduledAutomationTarget:
    entity_id: UUID
    payload: dict[str, object]


def _value(value: Any) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (UUID, date, datetime)):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    return value


def _target(row: Any, entity_id_field: str, **fields: Any) -> ScheduledAutomationTarget:
    entity_id = row.id
    payload = {key: _value(value) for key, value in fields.items() if value is not None}
    payload[entity_id_field] = str(entity_id)
    return ScheduledAutomationTarget(entity_id=entity_id, payload=payload)


def _active_rows(db: Session, model: Any) -> list[Any]:
    return list(
        db.scalars(
            select(model)
            .where(model.is_active.is_(True))
            .order_by(model.id)
            .limit(MAX_TARGETS_PER_PROVIDER)
        )
    )


def customer_accounts(db: Session) -> list[ScheduledAutomationTarget]:
    return [
        _target(row, "subscriber_id", status=row.status)
        for row in _active_rows(db, Subscriber)
    ]


def projects(db: Session) -> list[ScheduledAutomationTarget]:
    return [
        _target(row, "project_id", status=row.status, project_type=row.project_type)
        for row in _active_rows(db, Project)
    ]


def work_orders(db: Session) -> list[ScheduledAutomationTarget]:
    return [
        _target(row, "work_order_id", status=row.status)
        for row in _active_rows(db, WorkOrder)
    ]


def leads(db: Session) -> list[ScheduledAutomationTarget]:
    return [
        _target(row, "lead_id", status=row.status, pipeline_id=row.pipeline_id)
        for row in _active_rows(db, Lead)
    ]


def quotes(db: Session) -> list[ScheduledAutomationTarget]:
    return [
        _target(
            row,
            "quote_id",
            status=row.status,
            payment_review_status=row.payment_review_status,
        )
        for row in _active_rows(db, Quote)
    ]


def sales_orders(db: Session) -> list[ScheduledAutomationTarget]:
    return [
        _target(
            row, "sales_order_id", status=row.status, payment_status=row.payment_status
        )
        for row in _active_rows(db, SalesOrder)
    ]


def tickets(db: Session) -> list[ScheduledAutomationTarget]:
    return [
        _target(
            row,
            "ticket_id",
            status=row.status,
            priority=row.priority,
            channel=row.channel,
            ticket_type=row.ticket_type,
            region=row.region,
            customer_id=row.customer_account_id or row.subscriber_id,
        )
        for row in _active_rows(db, Ticket)
    ]


PROVIDERS: dict[str, Callable[[Session], list[ScheduledAutomationTarget]]] = {
    "customer.account": customer_accounts,
    "operations.project": projects,
    "operations.work_order": work_orders,
    "sales.lead": leads,
    "sales.quote": quotes,
    "sales.sales_order": sales_orders,
    "support.ticket": tickets,
}


def targets_for(db: Session, adapter_key: str) -> list[ScheduledAutomationTarget]:
    provider = PROVIDERS.get(adapter_key)
    if provider is None:
        raise ValueError(
            f"No scheduled automation provider is registered for {adapter_key!r}."
        )
    return provider(db)
