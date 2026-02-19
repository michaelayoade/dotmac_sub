from __future__ import annotations

import os
from dataclasses import dataclass


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class E2ESettings:
    base_url: str
    browser: str
    headless: bool
    slow_mo_ms: int
    action_timeout_ms: int
    navigation_timeout_ms: int
    expect_timeout_ms: int
    admin_username: str | None
    admin_password: str | None
    agent_username: str
    agent_password: str
    user_username: str
    user_password: str

    @classmethod
    def from_env(cls) -> E2ESettings:
        base_url = os.getenv("PLAYWRIGHT_BASE_URL", "http://localhost:8000").rstrip("/")
        browser = os.getenv("PLAYWRIGHT_BROWSER", "firefox")
        headless = _bool_env("PLAYWRIGHT_HEADLESS", True)
        slow_mo_ms = _int_env("PLAYWRIGHT_SLOW_MO", 0)
        action_timeout_ms = _int_env("PLAYWRIGHT_TIMEOUT_MS", 10000)
        navigation_timeout_ms = _int_env("PLAYWRIGHT_NAV_TIMEOUT_MS", 15000)
        expect_timeout_ms = _int_env("PLAYWRIGHT_EXPECT_TIMEOUT_MS", 10000)

        admin_username = os.getenv("E2E_ADMIN_USERNAME")
        admin_password = os.getenv("E2E_ADMIN_PASSWORD")
        agent_username = os.getenv("E2E_AGENT_USERNAME", "e2e.agent")
        agent_password = os.getenv("E2E_AGENT_PASSWORD", "AgentPass123!")
        user_username = os.getenv("E2E_USER_USERNAME", "e2e.user")
        user_password = os.getenv("E2E_USER_PASSWORD", "UserPass123!")

        return cls(
            base_url=base_url,
            browser=browser,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            action_timeout_ms=action_timeout_ms,
            navigation_timeout_ms=navigation_timeout_ms,
            expect_timeout_ms=expect_timeout_ms,
            admin_username=admin_username,
            admin_password=admin_password,
            agent_username=agent_username,
            agent_password=agent_password,
            user_username=user_username,
            user_password=user_password,
        )
