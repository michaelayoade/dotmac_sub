from __future__ import annotations

from pathlib import Path


def test_retry_task_rolls_back_before_logging_orm_event_fields() -> None:
    source = Path("app/tasks/events.py").read_text(encoding="utf-8")
    handler = source[source.index("def retry_failed_events") :]
    exception_block = handler[
        handler.index("except Exception as exc:") : handler.index(
            "result = {",
            handler.index("except Exception as exc:"),
        )
    ]

    assert exception_block.index("session.rollback()") < exception_block.index(
        "logger.exception"
    )
    assert "event_record.event_id" not in exception_block
