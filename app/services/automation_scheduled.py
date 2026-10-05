"""Record providers for scheduled Automation Center evaluations.

This owner atomically claims each due slot and stages target events. The task
adapter owns only session lifecycle; execution consumes the durable outbox.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.automation import (
    AutomationRule,
    AutomationRuleStatus,
    AutomationRuleVersion,
    AutomationScheduledRun,
)
from app.models.project import Project
from app.models.sales import Lead, Quote, SalesOrder
from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.services import automation_capabilities
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.timezone import APP_TIMEZONE_NAME

MAX_TARGETS_PER_PROVIDER = 2000


OWNER = "automation.scheduled_runs"
_ENQUEUE = OwnerCommandDefinition(
    owner=OWNER,
    concern="scheduled automation run claims",
    name="enqueue_scheduled_automation_events",
)


class ScheduledAutomationError(DomainError):
    pass


@dataclass(frozen=True, slots=True)
class EnqueueScheduledAutomationCommand:
    now: datetime
    context: CommandContext


@dataclass(frozen=True, slots=True)
class ScheduledAutomationOutcome:
    rules_claimed: int
    events_emitted: int
    rules_skipped: int


def _claim_slot(
    db: Session, *, rule: AutomationRule, version: AutomationRuleVersion, slot: str
) -> bool:
    """The database arbitrates duplicate claims without a nested transaction."""
    statement = (
        insert(AutomationScheduledRun)
        .values(
            tenant_id=rule.tenant_id,
            rule_id=rule.id,
            rule_version_id=version.id,
            slot_key=slot,
        )
        .on_conflict_do_nothing(index_elements=["rule_version_id", "slot_key"])
        .returning(AutomationScheduledRun.id)
    )
    return db.scalar(statement) is not None


@dataclass(frozen=True, slots=True)
class ScheduledAutomationTarget:
    entity_id: UUID
    payload: dict[str, object]


def _value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (UUID, date, datetime)):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    return value


def _target(
    entity_id: UUID, entity_id_field: str, **fields: object
) -> ScheduledAutomationTarget:
    payload = {key: _value(value) for key, value in fields.items() if value is not None}
    payload[entity_id_field] = str(entity_id)
    return ScheduledAutomationTarget(entity_id=entity_id, payload=payload)


RecordT = TypeVar(
    "RecordT", Subscriber, Project, WorkOrder, Lead, Quote, SalesOrder, Ticket
)


def _active_rows(db: Session, model: type[RecordT]) -> list[RecordT]:
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
        _target(row.id, "subscriber_id", status=row.status)
        for row in _active_rows(db, Subscriber)
    ]


def projects(db: Session) -> list[ScheduledAutomationTarget]:
    return [
        _target(row.id, "project_id", status=row.status, project_type=row.project_type)
        for row in _active_rows(db, Project)
    ]


def work_orders(db: Session) -> list[ScheduledAutomationTarget]:
    return [
        _target(row.id, "work_order_id", status=row.status)
        for row in _active_rows(db, WorkOrder)
    ]


def leads(db: Session) -> list[ScheduledAutomationTarget]:
    return [
        _target(row.id, "lead_id", status=row.status, pipeline_id=row.pipeline_id)
        for row in _active_rows(db, Lead)
    ]


def quotes(db: Session) -> list[ScheduledAutomationTarget]:
    return [
        _target(
            row.id,
            "quote_id",
            status=row.status,
            payment_review_status=row.payment_review_status,
        )
        for row in _active_rows(db, Quote)
    ]


def sales_orders(db: Session) -> list[ScheduledAutomationTarget]:
    return [
        _target(
            row.id,
            "sales_order_id",
            status=row.status,
            payment_status=row.payment_status,
        )
        for row in _active_rows(db, SalesOrder)
    ]


def tickets(db: Session) -> list[ScheduledAutomationTarget]:
    return [
        _target(
            row.id,
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


def _cron_part_matches(value: int, expression: str, minimum: int, maximum: int) -> bool:
    for part in expression.split(","):
        part = part.strip()
        if not part:
            return False
        base, _, step_text = part.partition("/")
        try:
            step = int(step_text) if step_text else 1
        except ValueError:
            return False
        if step < 1:
            return False
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            try:
                start, end = (int(item) for item in base.split("-", 1))
            except ValueError:
                return False
        else:
            try:
                start = end = int(base)
            except ValueError:
                return False
        if minimum <= start <= maximum and minimum <= end <= maximum:
            if start <= value <= end and (value - start) % step == 0:
                return True
    return False


def _cron_matches(expr: str, current: datetime) -> bool:
    parts = expr.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    return (
        _cron_part_matches(current.minute, minute, 0, 59)
        and _cron_part_matches(current.hour, hour, 0, 23)
        and _cron_part_matches(current.day, dom, 1, 31)
        and _cron_part_matches(current.month, month, 1, 12)
        and _cron_part_matches((current.weekday() + 1) % 7, dow, 0, 6)
    )


def _slot_for(schedule: Mapping[str, object], now: datetime) -> str | None:
    from zoneinfo import ZoneInfo

    timezone = str(schedule.get("timezone") or APP_TIMEZONE_NAME)
    current = now.astimezone(ZoneInfo(timezone))
    schedule_type = str(schedule.get("type") or "")
    if schedule_type == "interval":
        interval = int(str(schedule.get("interval_seconds") or "0"))
        if interval < 60:
            return None
        if interval >= 86400 and interval % 86400 == 0:
            day_bucket = current.toordinal() // (interval // 86400)
            return f"interval:{day_bucket}:{interval}"
        return f"interval:{int(current.timestamp()) // interval}:{interval}"
    if schedule_type == "crontab":
        cron_expr = str(schedule.get("cron_expr") or "")
        if not _cron_matches(cron_expr, current):
            return None
        return f"cron:{current:%Y%m%d%H%M}"
    return None


def enqueue_scheduled_events(
    db: Session, *, command: EnqueueScheduledAutomationCommand
) -> ScheduledAutomationOutcome:
    """Commit slot claims and their outbox events as one owner transaction."""
    if command.now.tzinfo is None:
        raise ScheduledAutomationError(
            code=f"{OWNER}.invalid_schedule_time",
            message="Scheduled automation requires an aware evaluation time.",
            retryable=False,
        )

    def operation() -> ScheduledAutomationOutcome:
        claimed = emitted = skipped = 0
        rows = db.execute(
            select(AutomationRule, AutomationRuleVersion)
            .join(
                AutomationRuleVersion,
                AutomationRuleVersion.id == AutomationRule.active_version_id,
            )
            .where(AutomationRule.status == AutomationRuleStatus.published.value)
            .order_by(AutomationRule.id)
        ).tuples()
        for rule, version in rows:
            slot = _slot_for(version.schedule or {}, command.now)
            if slot is None or not _claim_slot(
                db, rule=rule, version=version, slot=slot
            ):
                skipped += 1
                continue
            claimed += 1
            for trigger_key in tuple(rule.trigger_keys or [rule.trigger_key]):
                trigger = automation_capabilities.trigger_capability(trigger_key)
                if not trigger.scheduled or not trigger.schedule_adapter_key:
                    continue
                for target in targets_for(db, trigger.schedule_adapter_key):
                    event_id = uuid5(
                        NAMESPACE_URL,
                        f"dotmac:automation:{version.id}:{slot}:{trigger.key}:{target.entity_id}",
                    )
                    payload = {
                        **target.payload,
                        "name": trigger.event_type,
                        "tenant_id": str(rule.tenant_id),
                        "automation_rule_version_id": str(version.id),
                    }
                    emit_event(
                        db,
                        EventType.custom,
                        payload,
                        event_id=event_id,
                        actor=command.context.actor,
                        dispatch_after_commit=False,
                    )
                    emitted += 1
        return ScheduledAutomationOutcome(
            rules_claimed=claimed, events_emitted=emitted, rules_skipped=skipped
        )

    return execute_owner_command(
        db, definition=_ENQUEUE, context=command.context, operation=operation
    )
