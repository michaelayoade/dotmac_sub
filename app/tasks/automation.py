"""Periodic delivery for cross-module scheduled automation rules."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from app.celery_app import celery_app
from app.models.automation import (
    AutomationRule,
    AutomationRuleStatus,
    AutomationRuleVersion,
)
from app.services import automation_capabilities, automation_scheduled
from app.services.db_session_adapter import db_session_adapter
from app.services.events import EventType, emit_event
from app.timezone import APP_TIMEZONE_NAME

logger = logging.getLogger(__name__)


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


def _slot_for(schedule: dict[str, object], now: datetime) -> str | None:
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
        if interval >= 3600 and interval % 3600 == 0:
            return f"interval:{current:%Y%m%d%H}:{interval}"
        return f"interval:{int(current.timestamp()) // interval}:{interval}"
    if schedule_type == "crontab":
        cron_expr = str(schedule.get("cron_expr") or "")
        if not _cron_matches(cron_expr, current):
            return None
        return f"cron:{current:%Y%m%d%H%M}"
    return None


@celery_app.task(name="app.tasks.automation.run_scheduled_automation_rules")
def run_scheduled_automation_rules(*, now_iso: str | None = None) -> dict[str, int]:
    now = datetime.fromisoformat(now_iso) if now_iso else datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    claimed = emitted = skipped = 0
    with db_session_adapter.owner_command_session() as session:
        rows = session.execute(
            select(AutomationRule, AutomationRuleVersion)
            .join(
                AutomationRuleVersion,
                AutomationRuleVersion.id == AutomationRule.active_version_id,
            )
            .where(AutomationRule.status == AutomationRuleStatus.published.value)
            .order_by(AutomationRule.id)
        ).tuples()
        for rule, version in rows:
            slot = _slot_for(version.schedule or {}, now)
            if slot is None or not automation_scheduled.claim_slot(
                session, rule, version, slot
            ):
                skipped += 1
                continue
            claimed += 1
            for trigger_key in tuple(rule.trigger_keys or [rule.trigger_key]):
                trigger = automation_capabilities.trigger_capability(trigger_key)
                if not trigger.scheduled or not trigger.schedule_adapter_key:
                    continue
                targets = automation_scheduled.targets_for(
                    session, trigger.schedule_adapter_key
                )
                for target in targets:
                    event_id = uuid5(
                        NAMESPACE_URL,
                        f"dotmac:automation:{version.id}:{slot}:{trigger.key}:{target.entity_id}",
                    )
                    payload = {"name": trigger.event_type, **target.payload}
                    emit_event(
                        session,
                        EventType.custom,
                        payload,
                        event_id=event_id,
                        actor="automation-scheduler",
                    )
                    emitted += 1
    result = {
        "rules_claimed": claimed,
        "events_emitted": emitted,
        "rules_skipped": skipped,
    }
    logger.info("scheduled automation evaluation complete", extra=result)
    return result
