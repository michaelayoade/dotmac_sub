"""Tests for the admin overview attention-feed owner (pure decision function)."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

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
        suspended_count=0,
        pending_orders=0,
        unassigned_support_count=0,
        support_approaching_target_count=0,
        support_exceeded_target_count=0,
        onboarding_waiting_too_long_count=0,
        ont_summary={"low_signal": 0, "not_working": 0},
        unconfigured_ont_count=0,
        pending_location_requests=0,
        pon_outage_count=0,
    )
    kwargs.update(overrides)
    return build_attention_items(**kwargs)


def test_quiet_system_produces_no_attention_items():
    items, network_items = _build()
    assert items == [] and network_items == []


def test_items_are_severity_ordered_and_toned():
    items, network_items = _build(
        suspended_count=2,  # info
        unassigned_support_count=2,  # warning
        net_stats={**_NET_OK, "alarms_critical": 1},  # critical
    )
    assert [i["severity"] for i in items] == ["critical", "warning", "info"]
    assert items[0]["tone"] == "negative"
    assert items[1]["tone"] == "warning"
    assert items[2]["tone"] == "info"
    # only the network item lands in the network feed
    assert [i["severity"] for i in network_items] == ["critical"]


def test_ont_not_working_threshold_owned_here():
    items, _ = _build(ont_summary={"low_signal": 0, "not_working": 5})
    assert not any("not working" in i["label"] for i in items)
    items, _ = _build(ont_summary={"low_signal": 0, "not_working": 6})
    assert any("6 ONTs not working" == i["label"] for i in items)


def test_support_and_onboarding_attention_counts_are_rendered():
    items, _ = _build(
        unassigned_support_count=3,
        support_approaching_target_count=2,
        support_exceeded_target_count=1,
        onboarding_waiting_too_long_count=4,
    )
    labels = [i["label"] for i in items]
    assert "3 unassigned support tickets" in labels
    assert "2 support tickets approaching response target" in labels
    assert "1 support ticket past response target" in labels
    assert "4 customer onboarding cases waiting over 72 hours" in labels
    assert not any("infrastructure" in label for label in labels)
    assert not any("overdue receivables" in label for label in labels)

    unassigned = next(item for item in items if "unassigned support" in item["label"])
    query = parse_qs(urlparse(unassigned["href"]).query)
    assert query["status"] == ["not_closed"]
    assert json.loads(query["filters"][0]) == [
        ["Ticket", "assigned_to_person_id", "is", None]
    ]


def test_ont_not_working_threshold_is_configurable():
    # The significance threshold is a parameter (settings-owned at the caller);
    # the module constant is only the registered default.
    items, _ = _build(
        ont_summary={"low_signal": 0, "not_working": 3},
        ont_not_working_threshold=2,
    )
    assert any("3 ONTs not working" == i["label"] for i in items)
    items, _ = _build(
        ont_summary={"low_signal": 0, "not_working": 3},
        ont_not_working_threshold=10,
    )
    assert not any("not working" in i["label"] for i in items)
