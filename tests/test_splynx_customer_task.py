"""Tests for Splynx customer-only Celery task wrapper."""

from __future__ import annotations

from app.tasks import splynx_sync


def test_customer_accounts_details_task_executes_customer_only_sync(
    monkeypatch,
) -> None:
    expected = {
        "accounts": {"created": 1, "updated": 2, "mapped": 3, "skipped": 0},
        "custom_fields": {"created": 4, "updated": 5, "skipped": 6},
    }
    called: dict[str, bool] = {}

    def fake_run_customer_sync(*, dry_run: bool):
        called["dry_run"] = dry_run
        return expected

    import app.services.splynx_customer_sync as sync_module

    monkeypatch.setattr(sync_module, "run_customer_sync", fake_run_customer_sync)

    assert splynx_sync.run_customer_accounts_details_sync() == expected
    assert called == {"dry_run": False}
