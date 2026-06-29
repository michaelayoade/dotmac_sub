"""Tests for stale-Zabbix-host candidate selection."""

from scripts.one_off.audit_stale_zabbix_hosts import select_stale_candidates

_DEVICES = {
    "h_dead": {"name": "Dead AP", "mgmt_ip": "172.21.4.1", "subs": 0, "matched": None},
    "h_recent": {"name": "Recent", "mgmt_ip": "172.21.4.2", "subs": 0, "matched": None},
    "h_subs": {
        "name": "Has subs",
        "mgmt_ip": "172.16.0.9",
        "subs": 12,
        "matched": None,
    },
    "h_olt": {"name": "OLT", "mgmt_ip": "172.16.0.1", "subs": 0, "matched": "olt"},
    "h_up": {"name": "Healthy", "mgmt_ip": "172.16.0.2", "subs": 0, "matched": None},
}


def test_selects_only_long_dead_unmatched_zero_sub_hosts():
    ages = {
        "h_dead": 68.0,  # candidate
        "h_recent": 2.0,  # too recent
        "h_subs": 90.0,  # has subscribers -> keep
        "h_olt": 90.0,  # matched infra -> keep
        # h_up has no unreachable trigger at all -> not in ages
    }
    out = select_stale_candidates(_DEVICES, ages, min_days=30)
    assert [c.hostid for c in out] == ["h_dead"]
    assert out[0].name == "Dead AP"
    assert out[0].days_down == 68.0


def test_sorted_oldest_first():
    devices = {
        "a": {"name": "A", "mgmt_ip": None, "subs": 0, "matched": None},
        "b": {"name": "B", "mgmt_ip": None, "subs": 0, "matched": None},
    }
    out = select_stale_candidates(devices, {"a": 40.0, "b": 120.0}, min_days=30)
    assert [c.hostid for c in out] == ["b", "a"]


def test_min_days_threshold_respected():
    devices = {"x": {"name": "X", "mgmt_ip": None, "subs": 0, "matched": None}}
    assert select_stale_candidates(devices, {"x": 29.9}, min_days=30) == []
    assert len(select_stale_candidates(devices, {"x": 30.0}, min_days=30)) == 1


def test_unknown_host_in_ages_is_ignored():
    out = select_stale_candidates(_DEVICES, {"ghost": 99.0}, min_days=30)
    assert out == []
