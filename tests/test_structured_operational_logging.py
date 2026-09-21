import logging

from app.services.operational_logging import (
    OperationalEventName,
    OperationalLogEvent,
    OperationalOutcome,
    log_operational_event,
)
from app.tasks.notifications import _notification_queue_operational_event


def test_operational_summary_is_one_structured_info_event(caplog) -> None:
    with caplog.at_level(logging.INFO):
        log_operational_event(
            logging.getLogger("test.operational"),
            OperationalLogEvent(
                name=OperationalEventName.NOTIFICATION_QUEUE_PROCESSED,
                outcome=OperationalOutcome.COMPLETED_WITH_RETRIES,
                component="notifications",
                counters={"delivered": 4, "retried": 1, "failed": 0},
            ),
        )

    record = caplog.records[-1]
    assert record.getMessage() == "operational_task_outcome"
    assert record.event_name == "notification_queue_processed"
    assert record.outcome == "completed_with_retries"
    assert record.counters == {"delivered": 4, "retried": 1, "failed": 0}


def test_notification_queue_outcome_surfaces_staff_talk_failures() -> None:
    event = _notification_queue_operational_event(
        {
            "delivered": 11,
            "retried": 0,
            "failed": 0,
            "expired": 0,
            "rate_limited": 0,
            "talk_claimed": 9,
            "talk_delivered": 7,
            "talk_retried": 0,
            "talk_failed": 2,
            "talk_reconciled": 0,
        }
    )

    assert event.outcome is OperationalOutcome.COMPLETED_WITH_FAILURES
    assert event.counters["failed"] == 0
    assert event.counters["talk_failed"] == 2


def test_notification_queue_outcome_counts_staff_talk_retries() -> None:
    event = _notification_queue_operational_event(
        {
            "delivered": 0,
            "retried": 0,
            "failed": 0,
            "expired": 0,
            "rate_limited": 0,
            "talk_claimed": 1,
            "talk_delivered": 0,
            "talk_retried": 1,
            "talk_failed": 0,
            "talk_reconciled": 0,
        }
    )

    assert event.outcome is OperationalOutcome.COMPLETED_WITH_RETRIES
