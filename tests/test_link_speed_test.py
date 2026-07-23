"""TR-143 link speed test (G15).

Measures the router's throughput, not the browser's — the delta between the two
is what separates a customer WiFi problem from an access-link fault.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.network import link_speed_test as lst
from app.services.network.tr069_paths import Tr069PathResolver


class _FakeAcs:
    """Records what was asked of the device, and answers with canned values."""

    def __init__(self, values: dict | None = None):
        self.values = values or {}
        self.refreshed: list[tuple[str, str]] = []
        self.written: list[dict] = []

    def refresh_object(self, device_id, path, *, timeout_sec):
        self.refreshed.append((device_id, path))

    def set_parameter_values(self, device_id, params, *, timeout_sec):
        self.written.append(params)

    def get_parameter_values(self, device_id, paths):
        return {path: self.values.get(path) for path in paths}


TR098 = {
    "diag.download.state": "DownloadDiagnostics.DiagnosticsState",
    "diag.download.url": "DownloadDiagnostics.DownloadURL",
    "diag.download.test_bytes": "DownloadDiagnostics.TestBytesReceived",
    "diag.download.total_bytes": "DownloadDiagnostics.TotalBytesReceived",
    "diag.download.bom_time": "DownloadDiagnostics.BOMTime",
    "diag.download.eom_time": "DownloadDiagnostics.EOMTime",
}


# ---------------------------------------------------------------------------
# Throughput maths
# ---------------------------------------------------------------------------


def test_throughput_is_measured_between_first_and_last_byte():
    """TR-143 excludes setup/teardown: the window is BOMTime..EOMTime."""

    mbps, started, finished = lst.throughput_mbps(
        12_500_000, "2026-07-23T12:00:00.000000Z", "2026-07-23T12:00:10.000000Z"
    )

    assert mbps == 10.0  # 12.5 MB over 10s = 10 Mbps
    assert started == datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    assert finished == datetime(2026, 7, 23, 12, 0, 10, tzinfo=UTC)


def test_the_unset_time_sentinel_is_not_a_real_instant():
    """0001-01-01 is TR-143's "unknown"; treating it as a time invents a result."""

    mbps, started, _ = lst.throughput_mbps(
        12_500_000, "0001-01-01T00:00:00Z", "2026-07-23T12:00:10.000000Z"
    )

    assert mbps == 0.0
    assert started is None


@pytest.mark.parametrize(
    "byte_count,bom,eom",
    [
        (0, "2026-07-23T12:00:00Z", "2026-07-23T12:00:10Z"),
        (12_500_000, "2026-07-23T12:00:10Z", "2026-07-23T12:00:10Z"),
        ("not-a-number", "2026-07-23T12:00:00Z", "2026-07-23T12:00:10Z"),
        (12_500_000, "garbage", "2026-07-23T12:00:10Z"),
    ],
)
def test_an_unmeasurable_run_reports_zero_not_a_slow_link(byte_count, bom, eom):
    """An unmeasurable run is not a slow one; 0 Mbps must not libel a good link.

    Callers distinguish the two through diagnostics_state, never through mbps.
    """

    assert lst.throughput_mbps(byte_count, bom, eom)[0] == 0.0


# ---------------------------------------------------------------------------
# Arming
# ---------------------------------------------------------------------------


def test_arming_refreshes_the_object_before_writing_to_it():
    """The production ACS holds these objects with no enumerated children."""

    acs = _FakeAcs()

    path = lst.arm_link_speed_test(
        acs, "dev-1", TR098, target_url="http://speedtest.dotmac.ng/10mb"
    )

    assert acs.refreshed == [("dev-1", "DownloadDiagnostics")]
    assert path == "DownloadDiagnostics"
    # And the refresh happened before the set, not after.
    assert acs.written[0]["DownloadDiagnostics.DiagnosticsState"] == lst.STATE_REQUESTED


def test_arming_sets_the_target_url_and_requests_the_run():
    acs = _FakeAcs()

    lst.arm_link_speed_test(
        acs, "dev-1", TR098, target_url="http://speedtest.dotmac.ng/10mb"
    )

    written = acs.written[0]
    assert written["DownloadDiagnostics.DownloadURL"] == (
        "http://speedtest.dotmac.ng/10mb"
    )


def test_arming_a_tree_without_tr143_is_rejected():
    with pytest.raises(ValueError, match="No TR-143"):
        lst.arm_link_speed_test(_FakeAcs(), "dev-1", {}, target_url="http://x/")


# ---------------------------------------------------------------------------
# Harvesting
# ---------------------------------------------------------------------------


def test_a_still_running_test_yields_nothing_yet():
    acs = _FakeAcs({"DownloadDiagnostics.DiagnosticsState": lst.STATE_REQUESTED})

    assert lst.harvest_link_speed_test(acs, "dev-1", TR098) is None


def test_an_unreported_state_yields_nothing_yet():
    assert lst.harvest_link_speed_test(_FakeAcs({}), "dev-1", TR098) is None


def test_a_completed_run_is_returned_with_its_throughput():
    acs = _FakeAcs(
        {
            "DownloadDiagnostics.DiagnosticsState": lst.STATE_COMPLETE,
            "DownloadDiagnostics.TestBytesReceived": 25_000_000,
            "DownloadDiagnostics.BOMTime": "2026-07-23T12:00:00.000000Z",
            "DownloadDiagnostics.EOMTime": "2026-07-23T12:00:10.000000Z",
        }
    )

    result = lst.harvest_link_speed_test(acs, "dev-1", TR098)

    assert result.succeeded is True
    assert result.mbps == 20.0
    assert result.bytes_transferred == 25_000_000
    assert result.direction == "download"


def test_a_device_error_is_terminal_and_not_a_success():
    """Retrying Error_CannotResolveHostName only burns the customer's link again."""

    acs = _FakeAcs(
        {"DownloadDiagnostics.DiagnosticsState": "Error_CannotResolveHostName"}
    )

    result = lst.harvest_link_speed_test(acs, "dev-1", TR098)

    assert result is not None
    assert result.succeeded is False
    assert result.diagnostics_state.startswith(lst.TERMINAL_ERROR_PREFIX)
    assert result.mbps == 0.0


# ---------------------------------------------------------------------------
# Parameter tree wiring
# ---------------------------------------------------------------------------


def test_the_fleets_tr098_tree_resolves_the_tr143_paths():
    """363 of 406 production devices implement TR-143 through TR-098 only."""

    path = Tr069PathResolver().resolve("InternetGatewayDevice", "diag.download.state")

    assert path == "InternetGatewayDevice.DownloadDiagnostics.DiagnosticsState"


def test_the_tr181_tree_is_mapped_too_even_though_the_fleet_has_none():
    path = Tr069PathResolver().resolve("Device", "diag.download.state")

    assert path == "Device.IP.Diagnostics.DownloadDiagnostics.DiagnosticsState"


# ---------------------------------------------------------------------------
# Honest labelling of the browser-side test
# ---------------------------------------------------------------------------


def test_the_portal_test_says_what_it_actually_measures():
    """Presenting a Wi-Fi number as "your internet speed" manufactures disputes."""

    from pathlib import Path

    page = Path("templates/customer/services/speedtest.html").read_text()

    assert "over your Wi-Fi" in page
    assert "not the Dotmac line" in page
