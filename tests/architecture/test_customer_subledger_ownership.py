"""Posting groups have exactly one constructing writer (ADR 0007 Phase 3).

Every producer participates through the typed staging API inside its own
owner command; nothing else may construct posting rows, so a parallel
subledger writer cannot appear silently.
"""

from __future__ import annotations

from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"
_OWNER = _APP / "services" / "billing" / "customer_subledger.py"


def _offenders(needle: str) -> list[str]:
    hits: list[str] = []
    for path in _APP.rglob("*.py"):
        if path == _OWNER:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("class "):
                continue
            if needle in stripped:
                hits.append(str(path.relative_to(_APP)))
                break
    return sorted(hits)


def test_posting_rows_are_constructed_only_by_the_subledger_owner() -> None:
    assert _offenders("CustomerPostingGroup(") == [], (
        "CustomerPostingGroup rows are constructed only inside "
        "app/services/billing/customer_subledger.py; stage through the "
        "typed participant API instead."
    )
    assert _offenders("CustomerPositionEffect(") == [], (
        "CustomerPositionEffect rows are constructed only inside "
        "app/services/billing/customer_subledger.py."
    )
