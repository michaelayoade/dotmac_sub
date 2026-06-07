"""The destructive Playwright E2E suite must refuse to run against production.

Regression guard: the suite once ran against selfcare.dotmac.io and created ~38
"E2E Phase 1" catalog offers plus e2e test subscribers on the live system.
"""

from __future__ import annotations

import pytest

from tests.playwright.conftest import _guard_not_production


def test_guard_blocks_known_production_host(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_ALLOW_PRODUCTION", raising=False)
    with pytest.raises(pytest.fail.Exception):
        _guard_not_production("https://selfcare.dotmac.io/portal/dashboard")


def test_guard_allows_production_with_explicit_override(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_ALLOW_PRODUCTION", "1")
    # Must not raise when the operator opts in deliberately.
    _guard_not_production("https://selfcare.dotmac.io")


def test_guard_allows_local_and_staging_hosts(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_ALLOW_PRODUCTION", raising=False)
    _guard_not_production("http://localhost:8000")
    _guard_not_production("http://127.0.0.1:8001")
    _guard_not_production("https://staging.example.com")
