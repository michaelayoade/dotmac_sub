"""Prevent application adapters from gaining business transaction ownership."""

from __future__ import annotations

from collections import Counter

from scripts.architecture import sot_debt


def _current() -> Counter[tuple[str, str]]:
    return Counter(
        {
            (use.operation, use.path): use.count
            for use in sot_debt.adapter_transaction_uses()
        }
    )


def _format(entries: dict[tuple[str, str], int]) -> str:
    return "\n  ".join(
        f"{operation} {count} {path}"
        for (operation, path), count in sorted(entries.items())
    )


def test_no_new_or_expanded_adapter_transaction_ownership() -> None:
    current = _current()
    baseline = sot_debt.read_count_baseline(sot_debt.ADAPTER_TRANSACTION_BASELINE)
    expanded = {
        key: count for key, count in current.items() if count > baseline.get(key, 0)
    }

    assert not expanded, (
        "application adapters gained transaction operations. Delegate the "
        "business transaction to the registered public command owner; do not "
        "expand the migration baseline:\n  " + _format(expanded)
    )


def test_adapter_transaction_baseline_only_shrinks() -> None:
    current = _current()
    baseline = sot_debt.read_count_baseline(sot_debt.ADAPTER_TRANSACTION_BASELINE)
    resolved = {
        key: baseline_count - current.get(key, 0)
        for key, baseline_count in baseline.items()
        if current.get(key, 0) < baseline_count
    }

    assert not resolved, (
        "adapter transaction debt shrank; reduce or remove these baseline "
        "entries so the repair is permanent:\n  " + _format(resolved)
    )
