"""Bulk tariff changes stay inside the reviewed lifecycle owner boundary."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _assigned_attribute_names(source: str) -> set[str]:
    names: set[str] = set()

    def collect(target: ast.expr) -> None:
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
        elif isinstance(target, (ast.List, ast.Tuple)):
            for item in target.elts:
                collect(item)

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                collect(target)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            collect(node.target)
    return names


def test_bulk_tariff_service_has_no_parallel_subscription_writer() -> None:
    source = _source("app/services/bulk_tariff_change.py")

    assert "preview_subscription_batch(" in source
    assert "execute_subscription_batch(" in source
    assert "SubscriptionEffectiveTiming.next_cycle" in source
    assert "offer_id" not in _assigned_attribute_names(source)
    assert ".begin_nested(" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "apply_offer_radius_profile" not in source
    assert "reconcile_subscription_connectivity" not in source
    assert "update_subscription_sessions" not in source


def test_offer_writer_detector_distinguishes_assignment_from_comparison() -> None:
    assert "offer_id" in _assigned_attribute_names(
        "subscription.offer_id = target_offer_id"
    )
    assert "offer_id" not in _assigned_attribute_names(
        "subscription.offer_id == target_offer_id"
    )


def test_bulk_tariff_confirmation_carries_exact_review_evidence() -> None:
    template = _source("templates/admin/catalog/bulk_tariff_change.html")

    assert 'name="preview_fingerprint"' in template
    assert 'name="idempotency_key"' in template
    assert "applied immediately on confirm" not in template
    assert "next billing boundary" in template
