"""Tests for the reconciler state types.

These tests pin down dataclass immutability, the failure-reason enum surface,
and the round-trippability of the state types — nothing more. They exist mostly
to lock down the public type signatures so the rest of the package builds
against a stable contract.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from app.services.network.reconcile import (
    AcsObservedFields,
    AppliedAction,
    Drift,
    OltObservedFields,
    OntDesiredState,
    OntObservedState,
    ReconcileFailure,
    ReconcileFailureReason,
    ReconcileResult,
)


def _sample_desired(**overrides) -> OntDesiredState:
    """Build a minimal valid-looking OntDesiredState for tests."""
    defaults = dict(
        ont_unit_id="ont-1",
        serial_number="HWTC8535819A",
        olt_id="olt-spdc",
        fsp="0/1/3",
        olt_ont_id=11,
        line_profile_id=40,
        service_profile_id=42,
        description="Kolawole_Idiaro_2_zone_Zone_1_authd_20260512",
        mgmt_vlan=201,
        mgmt_ip="172.16.210.20",
        mgmt_subnet_mask="255.255.255.0",
        mgmt_gateway="172.16.210.1",
        mgmt_dns_primary="8.8.8.8",
        mgmt_dns_secondary="4.2.2.2",
        mgmt_iphost_priority=2,
        tr069_profile_id=2,
        acs_server_id="acs-dotmac",
        cr_username="admin",
        cr_password_ref="bao://acs/cr-password",
        periodic_inform_interval_sec=300,
        wan_mode="pppoe",
        wan_vlan=203,
        wan_gem_index=1,
        wan_pppoe_username="100024456",
        wan_pppoe_password_ref="bao://pppoe/100024456",
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
        wifi_ssid="KURSI",
        wifi_password_ref="bao://wifi/ont-1",
        wifi_password_pushed_at=None,
        mgmt_service_port_index=23,
        wan_service_port_index=22,
        subscriber_external_id=None,
        wan_uprate_kbps=None,
        wan_downrate_kbps=None,
    )
    defaults.update(overrides)
    return OntDesiredState(**defaults)


def _sample_observed() -> OntObservedState:
    return OntObservedState(
        last_reconciled_at=datetime(2026, 5, 12, 19, 33, 30, tzinfo=UTC),
        last_reconcile_duration_ms=8421,
        mgmt_ip_pingable=True,
        consecutive_sweep_unreachable=0,
        olt=OltObservedFields(
            olt_present=True,
            olt_match_state="match",
            olt_run_state="online",
            olt_distance_m=4374,
            olt_rx_dbm=-28.54,
            olt_tx_dbm=2.29,
            olt_temperature_c=40,
            olt_description="Kolawole_Idiaro_2_zone_Zone_1_authd_20260512",
            olt_mgmt_ip="172.16.210.20",
            olt_mgmt_vlan=201,
            olt_line_profile_id=40,
            olt_service_profile_id=42,
            olt_service_ports=(
                {"index": 22, "vlan": 203, "gem": 1, "state": "up"},
                {"index": 23, "vlan": 201, "gem": 2, "state": "up"},
            ),
        ),
        acs=AcsObservedFields(
            acs_present=True,
            acs_last_inform_at=datetime(2026, 5, 12, 19, 33, 30, tzinfo=UTC),
            acs_last_boot_at=datetime(2026, 5, 12, 19, 33, 30, tzinfo=UTC),
            acs_last_bootstrap_at=datetime(2026, 5, 12, 19, 28, 25, tzinfo=UTC),
            acs_observed_software_version="V5R019C10S100",
            acs_observed_pppoe_username="100024456",
            acs_observed_pppoe_enable=True,
            acs_observed_wan_vlan=203,
            acs_observed_wan_external_ip=None,
            acs_observed_wan_connection_status=None,
            acs_observed_nat_enabled=True,
            acs_observed_dhcp_enabled=True,
            acs_observed_ssid="KURSI",
            acs_observed_periodic_inform_interval_sec=300,
            acs_observed_cr_username_set=True,
            acs_observed_cr_password_set=True,
            acs_observed_wan_wcd_index=1,
            acs_observed_wan_instance_index=1,
        ),
    )


# ── Immutability ────────────────────────────────────────────────────────────


def test_ont_desired_state_is_frozen():
    """Desired state is immutable — proposed changes flow through reconcile_ont."""
    state = _sample_desired()
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.wifi_ssid = "NEW_SSID"  # type: ignore[misc]


def test_ont_observed_state_is_frozen():
    state = _sample_observed()
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.mgmt_ip_pingable = False  # type: ignore[misc]


def test_olt_observed_fields_are_frozen():
    state = _sample_observed()
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.olt.olt_run_state = "offline"  # type: ignore[misc]


def test_acs_observed_fields_are_frozen():
    state = _sample_observed()
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.acs.acs_observed_ssid = "OTHER"  # type: ignore[misc]


def test_reconcile_result_is_frozen():
    result = ReconcileResult(
        success=True,
        sync_status="synced",
        actions_applied=(),
        drift_before=(),
        drift_after=(),
        observed_after=_sample_observed(),
        failure=None,
        duration_ms=1234,
        reconciled_at=datetime(2026, 5, 13, 0, 0, tzinfo=UTC),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.success = False  # type: ignore[misc]


# ── Failure reason enum surface ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "constant_name",
    [
        "OLT_UNREACHABLE",
        "ACS_UNREACHABLE",
        "ONT_OFFLINE",
        "ONT_NOT_INFORMING",
        "BLOCKED_OUT_OF_SYNC",
        "INVALID_CHANGE",
        "OLT_WRITE_REJECTED",
        "ACS_WRITE_FAULTED",
        "ACS_CR_FAILED",
        "VERIFICATION_MISMATCH",
        "TIMEOUT",
    ],
)
def test_failure_reason_constant_exists_and_is_lowercase_string(constant_name):
    """Failure reason constants are lowercase strings safe to persist to DB and
    log without further normalization."""
    value = getattr(ReconcileFailureReason, constant_name)
    assert isinstance(value, str)
    assert value == value.lower()
    assert " " not in value


# ── Round-trip via dataclasses.replace ──────────────────────────────────────


def test_desired_state_supports_replace_for_proposed_changes():
    """Proposed changes will be built by callers via dataclasses.replace; the
    state type must support it for every field a caller might want to mutate."""
    state = _sample_desired()
    updated = dataclasses.replace(state, wifi_ssid="NEW_SSID", wifi_password_ref="ref")
    assert updated.wifi_ssid == "NEW_SSID"
    assert updated.wifi_password_ref == "ref"
    # The original is unchanged.
    assert state.wifi_ssid == "KURSI"


# ── Drift + AppliedAction shape ─────────────────────────────────────────────


def test_drift_and_applied_action_carry_minimal_payload():
    drift = Drift(
        field="wifi_ssid",
        surface="acs",
        desired="KURSI",
        observed="WirelessNet",
        repairable=True,
    )
    applied = AppliedAction(
        field="wifi_ssid",
        surface="acs",
        old_value="WirelessNet",
        new_value="KURSI",
        duration_ms=842,
    )
    assert drift.field == applied.field == "wifi_ssid"
    assert drift.surface == applied.surface == "acs"


def test_reconcile_failure_carries_reason_and_message():
    failure = ReconcileFailure(
        reason=ReconcileFailureReason.ACS_CR_FAILED,
        message="Connection request failed: empty CR credentials",
    )
    assert failure.reason == "acs_cr_failed"
    assert "Connection request" in failure.message
