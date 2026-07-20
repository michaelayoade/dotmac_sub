from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.event_store import failed_handler_names


def _attempt(name: str, status: str, retry_count: int, seconds: int):
    return SimpleNamespace(
        handler_name=name,
        status=status,
        retry_count=retry_count,
        attempted_at=datetime.now(UTC) + timedelta(seconds=seconds),
    )


def test_current_failure_manifest_overrides_historical_attempt_failures():
    record = SimpleNamespace(
        failed_handlers=[{"handler": "CurrentHandler", "error": "still failed"}],
        handler_attempts=[
            _attempt("RecoveredHandler", "failed", 0, 0),
            _attempt("RecoveredHandler", "success", 1, 1),
            _attempt("CurrentHandler", "failed", 1, 1),
        ],
    )

    assert failed_handler_names(record) == {"CurrentHandler"}


def test_legacy_attempt_fallback_uses_latest_status_per_handler():
    record = SimpleNamespace(
        failed_handlers=None,
        handler_attempts=[
            _attempt("RecoveredHandler", "failed", 0, 0),
            _attempt("RecoveredHandler", "success", 1, 1),
            _attempt("BlockedHandler", "blocked", 1, 1),
        ],
    )

    assert failed_handler_names(record) == {"BlockedHandler"}
