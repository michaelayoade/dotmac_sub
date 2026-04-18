"""Regression tests for Splynx incremental sync SQL compatibility."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from scripts.migration import incremental_sync


class _ScalarResult:
    def all(self) -> list:
        return []


class _FakeDb:
    def scalars(self, _stmt) -> _ScalarResult:
        return _ScalarResult()

    def flush(self) -> None:
        return None


def test_sync_new_payments_uses_existing_splynx_payment_columns(monkeypatch) -> None:
    queries: list[str] = []

    def fake_fetch_all(_conn, query: str, params=None) -> list[dict]:
        queries.append(query)
        if query == "SHOW COLUMNS FROM payments":
            return [
                {"Field": "id"},
                {"Field": "updated_at"},
                {"Field": "real_create_datetime"},
                {"Field": "payment_date"},
            ]
        return []

    monkeypatch.setattr(incremental_sync, "fetch_all", fake_fetch_all)

    result = incremental_sync.sync_new_payments(
        SimpleNamespace(),
        _FakeDb(),
        datetime(2026, 4, 18, 12, 30, tzinfo=UTC),
    )

    assert result == {"created": 0, "skipped": 0}
    assert queries[0] == "SHOW COLUMNS FROM payments"
    assert "payments" in queries[1]
    assert "payments.date" not in queries[1]
    assert "COALESCE(updated_at, real_create_datetime, payment_date)" in queries[1]


def test_sync_new_payments_supports_legacy_splynx_date_column(monkeypatch) -> None:
    queries: list[str] = []

    def fake_fetch_all(_conn, query: str, params=None) -> list[dict]:
        queries.append(query)
        if query == "SHOW COLUMNS FROM payments":
            return [
                {"Field": "id"},
                {"Field": "date"},
            ]
        return []

    monkeypatch.setattr(incremental_sync, "fetch_all", fake_fetch_all)

    result = incremental_sync.sync_new_payments(
        SimpleNamespace(),
        _FakeDb(),
        datetime(2026, 4, 18, 12, 30, tzinfo=UTC),
    )

    assert result == {"created": 0, "skipped": 0}
    assert "WHERE date >=" in queries[1]


def test_splynx_deleted_flag_normalization() -> None:
    assert incremental_sync._is_splynx_deleted(True) is True
    assert incremental_sync._is_splynx_deleted(1) is True
    assert incremental_sync._is_splynx_deleted("1") is True
    assert incremental_sync._is_splynx_deleted("true") is True
    assert incremental_sync._is_splynx_deleted(False) is False
    assert incremental_sync._is_splynx_deleted(0) is False
    assert incremental_sync._is_splynx_deleted("0") is False
