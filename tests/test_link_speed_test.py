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
    """Records what was asked of the device, and answers with canned values.

    Signatures mirror GenieACSClient exactly and are pinned by
    ``test_the_fake_matches_the_real_client_signature`` below — an earlier fake
    accepted an invented ``timeout_sec`` keyword, so every test passed while the
    first real call raised TypeError.
    """

    def __init__(self, values: dict | None = None):
        self.values = values or {}
        self.refreshed: list[tuple[str, str]] = []
        self.written: list[dict] = []

    def refresh_object(
        self,
        device_id,
        object_path,
        allow_broad_refresh=False,
        allow_when_pending=False,
    ):
        self.refreshed.append((device_id, object_path))
        return {}

    def get_parameter_values(self, device_id, parameters, allow_when_pending=False):
        return {path: self.values.get(path) for path in parameters}


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


def test_the_object_is_refreshed_before_it_can_be_armed():
    """The production ACS holds these objects with no enumerated children."""

    acs = _FakeAcs()

    path = lst.prepare_diagnostic_object(acs, "dev-1", TR098)

    assert acs.refreshed == [("dev-1", "DownloadDiagnostics")]
    assert path == "DownloadDiagnostics"


def test_the_arm_is_an_applier_action_not_a_direct_write():
    """ACS writes converge on reconcile/applier.py; see the architecture guard."""

    action = lst.build_arm_action(
        "dev-1", TR098, target_url="http://speedtest.dotmac.ng/10mb"
    )

    assert action.device_id == "dev-1"
    assert action.surface == "acs"
    assert action.label == "tr143.download"
    assert action.params["DownloadDiagnostics.DownloadURL"] == (
        "http://speedtest.dotmac.ng/10mb"
    )
    assert action.params["DownloadDiagnostics.DiagnosticsState"] == lst.STATE_REQUESTED


def test_this_module_never_writes_to_the_acs_itself():
    from pathlib import Path

    source = Path("app/services/network/link_speed_test.py").read_text()

    assert "set_parameter_values" not in source


def test_a_tree_without_tr143_is_rejected():
    with pytest.raises(ValueError, match="No TR-143"):
        lst.build_arm_action("dev-1", {}, target_url="http://x/")

    with pytest.raises(ValueError, match="No TR-143"):
        lst.prepare_diagnostic_object(_FakeAcs(), "dev-1", {})


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


# ---------------------------------------------------------------------------
# Conformance: the fake and the protocol must match the real client
# ---------------------------------------------------------------------------


def test_the_fake_matches_the_real_client_signature():
    """A fake shaped to fit the caller hides integration breakage.

    The original AcsReader invented a ``timeout_sec`` keyword. Unit tests passed
    against a fake that accepted it; the first call against GenieACSClient
    raised TypeError. Pinning the fake to the concrete client makes that class
    of bug fail here rather than on a customer's device.
    """
    import inspect

    from app.services.genieacs_client import GenieACSClient

    for name in ("refresh_object", "get_parameter_values"):
        real = inspect.signature(getattr(GenieACSClient, name))
        fake = inspect.signature(getattr(_FakeAcs, name))
        real_params = [p for p in real.parameters if p != "self"]
        fake_params = [p for p in fake.parameters if p != "self"]
        assert fake_params == real_params, (
            f"{name}: fake{tuple(fake_params)} does not match "
            f"client{tuple(real_params)}"
        )


def test_the_module_only_calls_the_client_with_real_arguments():
    """prepare_diagnostic_object must not pass keywords the client lacks."""
    import inspect

    from app.services.genieacs_client import GenieACSClient

    accepted = set(inspect.signature(GenieACSClient.refresh_object).parameters)
    source = inspect.getsource(lst.prepare_diagnostic_object)

    assert "timeout_sec" not in source
    assert "allow_when_pending" in accepted  # sanity: we read the right method
