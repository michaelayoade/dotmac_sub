"""Package and legacy limiters share slots in real PostgreSQL (PR-S1).

Two engines (``dotmac_device_cli.acquire_slot`` through the host's
``PgAdvisoryLockStore`` and ``olt_ssh_limiter.acquire_slot``) must contend on the
SAME advisory locks, or a swap would briefly allow twice the sessions. The unit
lane proves this against an in-memory lock server; this proves it against the
real database. Needs a disposable ``TEST_DATABASE_URL``; advisory locks leave
nothing behind and each test uses a fresh random OLT key.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from dotmac_device_cli import DeviceBusy, builtin_registry

from app.services.network import device_cli_host, olt_ssh_limiter
from app.services.network.olt_ssh_limiter import (
    LimiterConfig,
    OltSessionBusy,
    OltSshPriority,
    acquire_slot,
    set_config_override,
)


@pytest.fixture
def database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("slot-sharing proof needs TEST_DATABASE_URL (PostgreSQL)")
    return url


@pytest.fixture(autouse=True)
def _config():
    set_config_override(LimiterConfig())
    yield
    set_config_override(None)


def _olt() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), name="OLT-A", model="MA5608T")


def test_a_package_slot_is_seen_by_the_legacy_limiter_and_back(database_url) -> None:
    profile = builtin_registry().get("huawei-ma5608t-v800r013")
    assert profile is not None
    olt = _olt()
    key = str(olt.id)
    factory = device_cli_host.pg_store_factory(database_url)

    package_lease = device_cli_host.acquire_device_slot(
        olt, profile, priority=OltSshPriority.BACKGROUND, store_factory=factory
    )
    try:
        assert package_lease.slot_index == 0
        with pytest.raises(OltSessionBusy):
            acquire_slot(
                olt_key=key,
                limit=2,
                reserved=1,
                priority=OltSshPriority.BACKGROUND,
                wait_seconds=0,
                database_url=database_url,
            )
    finally:
        package_lease.release()

    legacy = acquire_slot(
        olt_key=key,
        limit=2,
        reserved=1,
        priority=OltSshPriority.BACKGROUND,
        wait_seconds=0,
        database_url=database_url,
    )
    try:
        with pytest.raises(DeviceBusy) as exc:
            device_cli_host.acquire_device_slot(
                olt, profile, priority=OltSshPriority.BACKGROUND, store_factory=factory
            )
        assert olt_ssh_limiter.is_busy_message(str(exc.value))
    finally:
        legacy.release()


def test_the_write_reserved_slot_is_shared_by_both_engines(database_url) -> None:
    """WRITE takes the reserved (highest) slot first in both engines, and the
    slot it takes is the one the other engine's BACKGROUND callers never use."""
    profile = builtin_registry().get("huawei-ma5608t-v800r013")
    assert profile is not None
    olt = _olt()
    key = str(olt.id)
    factory = device_cli_host.pg_store_factory(database_url)

    package_write = device_cli_host.acquire_device_slot(
        olt, profile, priority=OltSshPriority.WRITE, store_factory=factory
    )
    legacy_background = None
    try:
        assert package_write.slot_index == 1  # reserved slot (limit 2, reserved 1)
        legacy_background = acquire_slot(
            olt_key=key,
            limit=2,
            reserved=1,
            priority=OltSshPriority.BACKGROUND,
            wait_seconds=0,
            database_url=database_url,
        )
        assert legacy_background.slot_index == 0
        # Both slots held: a legacy WRITE and a package WRITE are both refused.
        with pytest.raises(OltSessionBusy):
            acquire_slot(
                olt_key=key,
                limit=2,
                reserved=1,
                priority=OltSshPriority.WRITE,
                wait_seconds=0,
                database_url=database_url,
            )
        with pytest.raises(DeviceBusy):
            device_cli_host.acquire_device_slot(
                olt, profile, priority=OltSshPriority.WRITE, store_factory=factory
            )
    finally:
        if legacy_background is not None:
            legacy_background.release()
        package_write.release()

    # A legacy WRITE holding the reserved slot is seen by the package WRITE,
    # which then takes the remaining slot.
    legacy_write = acquire_slot(
        olt_key=key,
        limit=2,
        reserved=1,
        priority=OltSshPriority.WRITE,
        wait_seconds=0,
        database_url=database_url,
    )
    try:
        assert legacy_write.slot_index == 1
        second = device_cli_host.acquire_device_slot(
            olt, profile, priority=OltSshPriority.WRITE, store_factory=factory
        )
        assert second.slot_index == 0
        second.release()
    finally:
        legacy_write.release()
