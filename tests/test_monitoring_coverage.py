"""Tests for monitoring-path coverage (Phase 3) + the no_path operational state.

See DEVICE_OPERATIONAL_STATUS.md / monitoring_coverage.py.
"""

from types import SimpleNamespace

from app.services.device_operational_status import (
    DOWN,
    UP,
    derive_operational_status,
)
from app.services.monitoring_coverage import MonitoringCoverage

# ── coverage object ──────────────────────────────────────────────────────────


def test_coverage_covers_ip_in_cidr():
    cov = MonitoringCoverage(["172.16.0.0/16", "10.10.0.2/32"], loaded=True)
    assert cov.covers("172.16.5.9") is True
    assert cov.covers("10.10.0.2") is True
    assert cov.covers("172.21.4.1") is False  # off-tunnel subnet


def test_unloaded_coverage_never_penalises():
    cov = MonitoringCoverage(None, loaded=False)
    assert cov.covers("172.21.4.1") is True  # no data -> don't penalise
    assert cov.covers(None) is True


def test_coverage_handles_bad_input():
    cov = MonitoringCoverage(["not-a-cidr", "172.16.0.0/16"], loaded=True)
    assert cov.cidr_count == 1
    assert cov.covers("garbage") is True  # unparseable ip -> don't penalise


def test_public_ip_is_covered_without_a_tunnel():
    # globally-routable mgmt IPs are reachable directly, not via a tunnel CIDR
    cov = MonitoringCoverage(["172.16.0.0/16"], loaded=True)
    assert cov.covers("160.119.127.5") is True  # public, not in any tunnel CIDR
    assert cov.covers("102.220.189.10") is True
    # private/tunnel-only address still needs a covering route
    assert cov.covers("172.21.4.1") is False
    # loopback is NOT global -> still falls through (a misconfigured host)
    assert cov.covers("127.0.0.1") is False


# ── deriver no_path integration ──────────────────────────────────────────────


def _dev(live, ip):
    return SimpleNamespace(status=None, live_status=live, mgmt_ip=ip)


def test_no_path_device_is_offline_while_retrying():
    cov = MonitoringCoverage(["172.16.0.0/16"], loaded=True)
    # device in an off-tunnel subnet, Zabbix says down -> blind spot
    op = derive_operational_status(
        _dev("down", "172.21.4.1"), warm_stale=False, coverage=cov
    )
    assert op.status == DOWN
    assert op.reason == "no_path_retry_pending"
    assert op.alarming is False


def test_covered_down_device_still_down():
    cov = MonitoringCoverage(["172.16.0.0/16"], loaded=True)
    op = derive_operational_status(
        _dev("down", "172.16.5.9"), warm_stale=False, coverage=cov
    )
    assert op.status == DOWN


def test_observed_up_wins_over_no_path():
    # a positive 'up' proves a path exists -> never call it no_path
    cov = MonitoringCoverage(["172.16.0.0/16"], loaded=True)
    op = derive_operational_status(
        _dev("up", "172.21.4.1"), warm_stale=False, coverage=cov
    )
    assert op.status == UP


def test_no_coverage_arg_is_phase1_behaviour():
    op = derive_operational_status(_dev("down", "172.21.4.1"), warm_stale=False)
    assert op.status == DOWN  # no coverage passed -> unchanged from Phase 1


# ── compute_reachable_cidrs degrades safely without wg ────────────────────────


def test_compute_reachable_cidrs_no_wg(monkeypatch):
    import app.services.monitoring_coverage as mc

    def _boom(*a, **k):
        raise OSError("no wg here")

    monkeypatch.setattr(mc.subprocess, "run", _boom)
    assert mc.compute_reachable_cidrs() == []


def test_compute_reachable_cidrs_parses_up_peer(monkeypatch):
    import time

    import app.services.monitoring_coverage as mc

    fresh = int(time.time())
    # `wg show all dump`: interface line (5 fields) + 1 up peer (9 fields)
    dump = (
        "wg0\tprivkey\tpubkey\t51820\toff\n"
        f"wg0\tpeerpub\t(none)\t1.2.3.4:51820\t172.16.0.0/16,10.10.0.2/32\t{fresh}\t1\t2\t0\n"
        "wg0\tstalepub\t(none)\t5.6.7.8:51820\t172.99.0.0/16\t1000\t1\t2\t0\n"
    )
    monkeypatch.setattr(
        mc.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=dump),
    )
    cidrs = mc.compute_reachable_cidrs()
    assert "172.16.0.0/16" in cidrs
    assert "10.10.0.2/32" in cidrs
    assert "172.99.0.0/16" not in cidrs  # stale handshake -> excluded
