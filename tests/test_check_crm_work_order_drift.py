"""Phase 2 work-order drift checker: status classification + native tolerance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scripts.migration.check_crm_ticket_drift import in_live_window
from scripts.migration.check_crm_work_order_drift import (
    TERMINAL_STATUSES,
    classify_status,
    is_native_row,
    is_open_status,
)

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)


def test_terminal_vocabulary_matches_mirror_semantics():
    assert TERMINAL_STATUSES == {"completed", "canceled"}
    assert is_open_status("in_progress") is True
    assert is_open_status("paused") is True
    assert is_open_status("completed") is False
    assert is_open_status("CANCELED") is False


def test_native_rows_recognized_by_sub_prefix():
    assert is_native_row("sub-3f2a") is True
    assert is_native_row("7e6a2f0c-0000-0000-0000-000000000000") is False
    assert is_native_row(None) is False


def test_classify_status_ok_when_equal():
    assert classify_status("scheduled", "scheduled", native_field_source=None) == "ok"
    assert (
        classify_status("In_Progress", "in_progress", native_field_source=None) == "ok"
    )


def test_classify_status_native_precedence_is_tolerated():
    """Sub's field services own the row: a CRM disagreement is the expected
    consequence of reconcile-clobber protection, not drift."""
    assert (
        classify_status("scheduled", "in_progress", native_field_source="sub")
        == "native_precedence"
    )
    assert (
        classify_status("in_progress", "completed", native_field_source="sub")
        == "native_precedence"
    )


def test_classify_status_drift_without_native_marker():
    assert (
        classify_status("scheduled", "in_progress", native_field_source=None) == "drift"
    )
    assert (
        classify_status("completed", "in_progress", native_field_source="") == "drift"
    )


def test_in_live_window_reused_from_ticket_mold():
    recent = NOW - timedelta(minutes=5)
    old = NOW - timedelta(hours=2)
    assert in_live_window(recent, now=NOW, window_minutes=30) is True
    assert in_live_window(old, now=NOW, window_minutes=30) is False
    assert in_live_window(None, now=NOW, window_minutes=30) is False
