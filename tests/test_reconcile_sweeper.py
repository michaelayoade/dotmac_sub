"""Tests for the reconciler sweeper.

Stub ``reconcile_ont`` so we exercise the sweep orchestration (per-ONT
loop, reachability fast-fail, error isolation, stats aggregation) without
hitting real OLT/ACS. Uses the project's ``db_session`` fixture for real
``OntUnit`` rows.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.network import OLTDevice, OntSyncStatus, OntUnit
from app.services.network.reconcile import (
    OntDesiredState,
    ReconcileFailure,
    ReconcileFailureReason,
    ReconcileResult,
)
from app.services.network.reconcile.sweeper import (
    SweepStats,
    run_sweep_once,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def olt_device(db_session):
    olt = OLTDevice(
        name="OLT-SWEEP-TEST",
        mgmt_ip="172.20.100.30",
        is_active=True,
    )
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)
    return olt


@pytest.fixture
def two_onts(db_session, olt_device):
    onts = [
        OntUnit(
            serial_number=f"HWTC0000000{i}",
            olt_device_id=olt_device.id,
            board="0/1",
            port=str(i + 1),
            external_id=str(i),
            is_active=True,
            sync_status=OntSyncStatus.synced,
        )
        for i in range(2)
    ]
    db_session.add_all(onts)
    db_session.commit()
    for ont in onts:
        db_session.refresh(ont)
    return onts


@pytest.fixture
def db_factory(db_session):
    """A factory the sweeper can call to get a fresh session — for tests we
    return the same session each time (no per-ONT isolation needed)."""
    from contextlib import contextmanager

    @contextmanager
    def _factory():
        try:
            yield db_session
        finally:
            pass

    return _factory


def _make_desired(ont) -> OntDesiredState:
    """Minimal valid desired state for sweeper tests."""
    return OntDesiredState(
        ont_unit_id=str(ont.id),
        serial_number=ont.serial_number,
        olt_id=str(ont.olt_device_id),
        fsp=f"0/1/{ont.port}",
        olt_ont_id=int(ont.external_id),
        line_profile_id=40,
        service_profile_id=42,
        description="x",
        mgmt_vlan=201,
        mgmt_ip="172.16.210.20",
        mgmt_subnet_mask="255.255.255.0",
        mgmt_gateway="172.16.210.1",
        mgmt_dns_primary="8.8.8.8",
        mgmt_dns_secondary="4.2.2.2",
        mgmt_iphost_priority=2,
        tr069_profile_id=2,
        acs_server_id="x",
        cr_username="admin",
        cr_password_ref="x",
        periodic_inform_interval_sec=300,
        wan_mode="pppoe",
        wan_vlan=203,
        wan_gem_index=1,
        wan_pppoe_username="x",
        wan_pppoe_password_ref="x",
        wan_pppoe_provisioning_method="tr069",
        wan_pppoe_wcd_index=1,
        wan_pppoe_instance_index=1,
        wan_config_profile_id=None,
        wan_internet_config_ip_index=None,
        nat_enabled=True,
        ipv6_enabled=False,
        dhcp_enabled=True,
        dhcp_pool_min="192.168.100.2",
        dhcp_pool_max="192.168.100.254",
        dhcp_subnet_mask="255.255.255.0",
        wifi_ssid="x",
        wifi_password_ref="x",
        wifi_password_pushed_at=None,
        mgmt_service_port_index=None,
        wan_service_port_index=None,
        subscriber_external_id=None,
        wan_uprate_kbps=None,
        wan_downrate_kbps=None,
    )


def _stub_result(success: bool) -> ReconcileResult:
    return ReconcileResult(
        success=success,
        sync_status="synced" if success else "out_of_sync",
        actions_applied=(),
        drift_before=(),
        drift_after=(),
        observed_after=None,
        failure=(
            None
            if success
            else ReconcileFailure(
                reason=ReconcileFailureReason.OLT_WRITE_REJECTED,
                message="rejected",
            )
        ),
        duration_ms=1,
        reconciled_at=datetime.now(UTC),
    )


# ── run_sweep_once ──────────────────────────────────────────────────────────


def test_sweep_skips_unreachable_ont_without_invoking_reconcile(
    db_session, two_onts, db_factory, monkeypatch
):
    reconcile_calls: list = []

    monkeypatch.setattr(
        "app.services.network.reconcile.sweeper.desired_from_ont_unit",
        lambda db, ont: _make_desired(ont),
    )

    def _fake_reconcile(*a, **k):
        reconcile_calls.append(k)
        return _stub_result(True)

    stats = run_sweep_once(
        db_factory,
        ping_function=lambda ip, count, timeout_sec: False,  # all unreachable
        reconcile_fn=_fake_reconcile,
    )

    assert stats.total_onts == 2
    assert stats.skipped_unreachable == 2
    assert stats.reconciled == 0
    assert reconcile_calls == []  # never invoked
    # Each ONT's counter incremented
    for ont in two_onts:
        db_session.refresh(ont)
        assert ont.consecutive_sweep_unreachable == 1


def test_sweep_invokes_reconcile_for_reachable_onts(
    db_session, two_onts, db_factory, monkeypatch
):
    reconcile_calls: list = []

    monkeypatch.setattr(
        "app.services.network.reconcile.sweeper.desired_from_ont_unit",
        lambda db, ont: _make_desired(ont),
    )

    def _fake_reconcile(db, ont_unit_id, **k):
        reconcile_calls.append(ont_unit_id)
        return _stub_result(True)

    stats = run_sweep_once(
        db_factory,
        ping_function=lambda ip, count, timeout_sec: True,
        reconcile_fn=_fake_reconcile,
    )

    assert stats.total_onts == 2
    assert stats.reconciled == 2
    assert stats.succeeded == 2
    assert stats.failed == 0
    assert len(reconcile_calls) == 2


def test_sweep_counts_failures_independently_of_successes(
    db_session, two_onts, db_factory, monkeypatch
):
    monkeypatch.setattr(
        "app.services.network.reconcile.sweeper.desired_from_ont_unit",
        lambda db, ont: _make_desired(ont),
    )

    outcomes = iter([True, False])

    def _fake_reconcile(*a, **k):
        return _stub_result(next(outcomes))

    stats = run_sweep_once(
        db_factory,
        ping_function=lambda ip, count, timeout_sec: True,
        reconcile_fn=_fake_reconcile,
    )

    assert stats.reconciled == 2
    assert stats.succeeded == 1
    assert stats.failed == 1


def test_sweep_isolates_per_ont_exceptions(
    db_session, two_onts, db_factory, monkeypatch
):
    """One ONT raising should not abort the sweep — the next ONT proceeds
    and the error is captured in stats."""
    monkeypatch.setattr(
        "app.services.network.reconcile.sweeper.desired_from_ont_unit",
        lambda db, ont: _make_desired(ont),
    )

    call_count = {"n": 0}

    def _flaky_reconcile(db, ont_unit_id, **k):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("first ont blew up")
        return _stub_result(True)

    stats = run_sweep_once(
        db_factory,
        ping_function=lambda ip, count, timeout_sec: True,
        reconcile_fn=_flaky_reconcile,
    )

    assert stats.total_onts == 2
    assert len(stats.errors) == 1
    assert "blew up" in stats.errors[0]
    # The second ONT still got reconciled
    assert stats.succeeded == 1


def test_sweep_only_active_filters_inactive_onts(
    db_session, olt_device, db_factory, monkeypatch
):
    """only_active=True (default) skips ONTs with is_active=False."""
    active = OntUnit(
        serial_number="ACTIVE",
        olt_device_id=olt_device.id,
        is_active=True,
        sync_status=OntSyncStatus.synced,
    )
    inactive = OntUnit(
        serial_number="INACTIVE",
        olt_device_id=olt_device.id,
        is_active=False,
        sync_status=OntSyncStatus.synced,
    )
    db_session.add_all([active, inactive])
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.reconcile.sweeper.desired_from_ont_unit",
        lambda db, ont: _make_desired(ont),
    )

    stats = run_sweep_once(
        db_factory,
        ping_function=lambda ip, count, timeout_sec: True,
        reconcile_fn=lambda *a, **k: _stub_result(True),
    )

    assert stats.total_onts == 1  # only the active one


def test_sweep_stats_carry_durations_and_timestamps():
    """``SweepStats`` is a frozen-ish dataclass; check the computed property."""
    stats = SweepStats(started_at=datetime(2026, 5, 13, 0, 0, tzinfo=UTC))
    stats.completed_at = datetime(2026, 5, 13, 0, 5, tzinfo=UTC)
    assert stats.duration_sec == 300.0


def test_sweep_resets_unreachable_counter_via_reconcile(
    db_session, two_onts, db_factory, monkeypatch
):
    """When the sweep succeeds against a reachable ONT, reconcile_ont's
    success path resets ``consecutive_sweep_unreachable`` (proven in
    test_reconcile_core; sanity-check it composes through the sweeper)."""
    # Pre-seed the counter so we'd see if reconcile resets it
    two_onts[0].consecutive_sweep_unreachable = 3
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.reconcile.sweeper.desired_from_ont_unit",
        lambda db, ont: _make_desired(ont),
    )

    def _resetting_reconcile(db, ont_unit_id, **k):
        # Simulate what real reconcile_ont does on success
        from sqlalchemy import select

        ont = db.execute(
            select(OntUnit).where(OntUnit.id == ont_unit_id)
        ).scalar_one()
        ont.consecutive_sweep_unreachable = 0
        return _stub_result(True)

    run_sweep_once(
        db_factory,
        ping_function=lambda ip, count, timeout_sec: True,
        reconcile_fn=_resetting_reconcile,
    )

    db_session.refresh(two_onts[0])
    assert two_onts[0].consecutive_sweep_unreachable == 0
