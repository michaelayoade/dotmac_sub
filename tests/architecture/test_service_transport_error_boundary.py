"""Keep HTTP transport errors out of new and migrated domain services."""

from __future__ import annotations

from scripts.architecture import sot_debt


def test_no_new_service_http_exception_coupling() -> None:
    current = sot_debt.service_http_exception_files()
    baseline = sot_debt.read_name_baseline(sot_debt.HTTP_EXCEPTION_BASELINE)
    new = sorted(current - baseline)

    assert not new, (
        "service modules gained FastAPI HTTPException coupling. Raise a typed "
        "domain error and map it in the web/API adapter; do not expand the "
        "migration baseline:\n  " + "\n  ".join(new)
    )


def test_service_http_exception_baseline_only_shrinks() -> None:
    current = sot_debt.service_http_exception_files()
    baseline = sot_debt.read_name_baseline(sot_debt.HTTP_EXCEPTION_BASELINE)
    resolved = sorted(baseline - current)

    assert not resolved, (
        "service HTTPException debt was resolved; remove these entries from "
        "the shrink-only baseline:\n  " + "\n  ".join(resolved)
    )
