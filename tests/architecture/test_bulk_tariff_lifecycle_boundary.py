"""Bulk tariff changes stay inside the reviewed lifecycle owner boundary."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_bulk_tariff_service_has_no_parallel_subscription_writer() -> None:
    source = _source("app/services/bulk_tariff_change.py")

    assert "preview_subscription_batch(" in source
    assert "execute_subscription_batch(" in source
    assert "SubscriptionEffectiveTiming.next_cycle" in source
    assert re.search(r"\.offer_id\s*=(?!=)", source) is None
    assert ".begin_nested(" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "apply_offer_radius_profile" not in source
    assert "reconcile_subscription_connectivity" not in source
    assert "update_subscription_sessions" not in source


def test_bulk_tariff_confirmation_carries_exact_review_evidence() -> None:
    template = _source("templates/admin/catalog/bulk_tariff_change.html")

    assert 'name="preview_fingerprint"' in template
    assert 'name="idempotency_key"' in template
    assert "applied immediately on confirm" not in template
    assert "next billing boundary" in template
