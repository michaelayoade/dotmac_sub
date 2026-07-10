"""Source-of-truth metadata helpers for native field writes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.models.work_order_mirror import WorkOrderMirror

SUB_AUTHORITATIVE_SOURCE = "sub"


def mark_sub_authoritative(
    row: WorkOrderMirror,
    activity: str,
    *,
    details: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> None:
    metadata = dict(row.metadata_ or {})
    activity_log = dict(metadata.get("native_field_activity") or {})
    happened_at = occurred_at or datetime.now(UTC)
    activity_log[activity] = {
        "source": SUB_AUTHORITATIVE_SOURCE,
        "occurred_at": happened_at.isoformat(),
        **(details or {}),
    }
    metadata["native_field_source"] = SUB_AUTHORITATIVE_SOURCE
    metadata["native_field_activity"] = activity_log
    row.metadata_ = metadata
