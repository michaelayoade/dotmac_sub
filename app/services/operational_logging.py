"""Typed operational task outcome observations."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class OperationalEventName(StrEnum):
    """Closed event names for task summaries that are safe to aggregate."""

    BILLING_ENFORCEMENT_COMPLETED = "billing_enforcement_completed"
    NOTIFICATION_QUEUE_PROCESSED = "notification_queue_processed"
    ERP_SYNC_EVENTS_COMPLETED = "erp_sync_events_completed"
    ERP_OPERATIONAL_SYNC_COMPLETED = "erp_operational_sync_completed"
    UISP_TOPOLOGY_SYNC_COMPLETED = "uisp_topology_sync_completed"
    ERP_EXPENSE_STATUS_REFRESH_COMPLETED = "erp_expense_status_refresh_completed"
    ERP_MATERIAL_STATUS_REFRESH_COMPLETED = "erp_material_status_refresh_completed"
    ERP_PURCHASE_INVOICE_STATUS_REFRESH_COMPLETED = (
        "erp_purchase_invoice_status_refresh_completed"
    )


class OperationalOutcome(StrEnum):
    COMPLETED = "completed"
    COMPLETED_WITH_RETRIES = "completed_with_retries"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"

    @property
    def recording_status(self) -> str:
        """Serialize into the existing heartbeat/metric vocabulary."""
        return {
            OperationalOutcome.COMPLETED: "success",
            OperationalOutcome.COMPLETED_WITH_RETRIES: "retryable",
            OperationalOutcome.PARTIAL: "partial",
            OperationalOutcome.FAILED: "error",
            OperationalOutcome.BLOCKED: "blocked",
            OperationalOutcome.SKIPPED: "skipped",
        }[self]


@dataclass(frozen=True, slots=True)
class OperationalBatchCounts:
    """One batch's attempted and failed records, not business eligibility."""

    processed: int
    failed: int

    @property
    def outcome(self) -> OperationalOutcome:
        if self.failed:
            return (
                OperationalOutcome.FAILED
                if self.failed >= self.processed
                else OperationalOutcome.PARTIAL
            )
        if not self.processed:
            return OperationalOutcome.SKIPPED
        return OperationalOutcome.COMPLETED


@dataclass(frozen=True)
class OperationalLogEvent:
    """One compact task-summary event, without message-text classification."""

    name: OperationalEventName
    outcome: OperationalOutcome
    component: str
    counters: Mapping[str, int]


def log_operational_event(
    event_logger: logging.Logger, event: OperationalLogEvent
) -> None:
    """Emit the caller's typed business outcome, not framework completion.

    Expected retries and skips remain INFO; blocked/partial work is WARNING.
    Only an explicitly failed outcome is ERROR. No message-text classification
    or automatic replay of partially committed work is introduced.
    """

    level = (
        logging.ERROR
        if event.outcome is OperationalOutcome.FAILED
        else logging.WARNING
        if event.outcome in {OperationalOutcome.PARTIAL, OperationalOutcome.BLOCKED}
        else logging.INFO
    )
    event_logger.log(
        level,
        "operational_task_outcome",
        extra={
            "event_name": event.name.value,
            "outcome": event.outcome.value,
            "component": event.component,
            "counters": dict(event.counters),
        },
    )
