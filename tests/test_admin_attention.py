"""Tests for the admin overview attention-feed owner (pure decision function)."""

from __future__ import annotations

from app.services.admin_attention import build_attention_items

_NET_OK = {
    "alarms_critical": 0,
    "alarms_major": 0,
    "alarms_minor": 0,
    "alarms_warning": 0,
    "offline_count": 0,
}


def _build(**overrides):
    kwargs = dict(
        net_stats=_NET_OK,
        overdue_amount=0.0,
        suspended_count=0,
        pending_orders=0,
        ont_summary={"low_signal": 0, "offline": 0},
        unconfigured_ont_count=0,
        pending_location_requests=0,
        pon_outage_count=0,
        infrastructure_alerts=None,
    )
    kwargs.update(overrides)
    return build_attention_items(**kwargs)


def test_quiet_system_produces_no_attention_items():
    items, network_items = _build()
    assert items == [] and network_items == []


def test_items_are_severity_ordered_and_toned():
    items, network_items = _build(
        suspended_count=2,  # info
        overdue_amount=1000.0,  # warning
        net_stats={**_NET_OK, "alarms_critical": 1},  # critical
    )
    assert [i["severity"] for i in items] == ["critical", "warning", "info"]
    assert items[0]["tone"] == "negative"
    assert items[1]["tone"] == "warning"
    assert items[2]["tone"] == "info"
    # only the network item lands in the network feed
    assert [i["severity"] for i in network_items] == ["critical"]


def test_ont_offline_threshold_owned_here():
    items, _ = _build(ont_summary={"low_signal": 0, "offline": 5})
    assert not any("offline" in i["label"] for i in items)
    items, _ = _build(ont_summary={"low_signal": 0, "offline": 6})
    assert any("6 ONTs offline" == i["label"] for i in items)


def test_absorbed_template_items_pon_and_infra():
    items, _ = _build(
        pon_outage_count=2,
        infrastructure_alerts={"total": 3, "critical": 1},
    )
    labels = [i["label"] for i in items]
    assert "2 PON ports down" in labels
    assert "3 infrastructure alerts (1 critical)" in labels
    # infra severity decision: critical only when critical alerts exist
    items, _ = _build(infrastructure_alerts={"total": 2, "critical": 0})
    infra = next(i for i in items if "infrastructure" in i["label"])
    assert infra["severity"] == "warning"
