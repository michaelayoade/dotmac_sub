"""Rate-math correctness for the Zabbix metrics engine.

Covers 32-bit counter rollover correction, implausible-spike / reset dropping,
and rate-vs-counter handling of trend value_avg.
"""

from __future__ import annotations

from app.services.zabbix_engine import CounterSample, ZabbixMetricsEngine


def _engine() -> ZabbixMetricsEngine:
    # A non-None client skips ZabbixClient.from_env(); these tests only exercise
    # pure rate math and never call the client.
    return ZabbixMetricsEngine(client=object())


def test_rate_point_normal_byte_counter():
    e = _engine()
    item = {"itemid": "1"}  # no units -> treated as a byte counter
    prev = CounterSample(item_id="1", clock=0, value=0.0)
    cur = CounterSample(item_id="1", clock=10, value=1250.0)  # 1250 B / 10s
    point = e._rate_point(item, cur, prev, "in")
    assert point is not None
    assert abs(point.bps - 1000.0) < 1e-6  # 1250*8/10


def test_rate_point_32bit_rollover_is_corrected():
    e = _engine()
    item = {"itemid": "1"}
    prev = CounterSample(item_id="1", clock=0, value=float(2**32 - 1000))
    cur = CounterSample(item_id="1", clock=10, value=250.0)  # wrapped past 2^32
    point = e._rate_point(item, cur, prev, "in")
    # corrected delta = (250 - (2^32-1000)) + 2^32 = 1250 bytes -> 1000 bps
    assert point is not None
    assert abs(point.bps - 1000.0) < 1e-6


def test_rate_point_counter_reset_is_dropped():
    e = _engine()
    item = {"itemid": "1"}
    # A 64-bit counter reset (reboot): the +2^32 wrap guess stays negative, so
    # the point is dropped rather than emitting a garbage value.
    prev = CounterSample(item_id="1", clock=0, value=5_000_000_000.0)
    cur = CounterSample(item_id="1", clock=10, value=100.0)
    assert e._rate_point(item, cur, prev, "in") is None


def test_rate_point_implausible_spike_is_dropped():
    e = _engine()
    item = {"itemid": "1"}
    prev = CounterSample(item_id="1", clock=0, value=0.0)
    cur = CounterSample(item_id="1", clock=1, value=1e18)  # 8e18 bps > ceiling
    assert e._rate_point(item, cur, prev, "in") is None


def test_trend_rate_item_uses_value_avg_directly():
    e = _engine()
    item = {"itemid": "1", "units": "bps", "key_": "net.if.in[eth0]"}
    assert e._units_are_rate(item) is True
    point = e._rate_point_from_trend_avg(
        item, {"itemid": "1", "clock": 100, "value_avg": 2_000_000}
    )
    assert point is not None
    # value_avg is already bits/s for a rate item -> used directly, not differenced
    assert abs(point.bps - 2_000_000.0) < 1e-6


def test_counter_units_are_not_rate():
    e = _engine()
    assert e._units_are_rate({"units": "B"}) is False
    assert e._units_are_rate({"units": "octets"}) is False
