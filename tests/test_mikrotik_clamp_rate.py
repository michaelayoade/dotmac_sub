"""Queue-rate clamp guards the bandwidth "peak" against RouterOS glitches.

A simple queue's reported ``rate`` cannot physically exceed its configured
``max-limit``; a higher reading is a measurement glitch that would otherwise
become a bogus peak. _clamp_rate bounds each sample to the line rate.
"""

from app.poller.mikrotik_poller import _clamp_rate


def test_within_cap_unchanged():
    assert _clamp_rate(40_000_000, 100_000_000) == 40_000_000


def test_glitch_above_cap_clamped_to_cap():
    # 491 Mbps reported on a 100 Mbps queue -> clamped to the cap.
    assert _clamp_rate(491_000_000, 100_000_000) == 100_000_000


def test_small_overshoot_within_tolerance_kept():
    # Just over the cap (rounding) stays — tolerance absorbs it.
    assert _clamp_rate(102_000_000, 100_000_000) == 102_000_000


def test_unlimited_cap_passes_through():
    # max-limit "0/0" (unlimited) -> no clamp.
    assert _clamp_rate(491_000_000, 0) == 491_000_000


def test_negative_rate_floored_to_zero():
    assert _clamp_rate(-5, 100_000_000) == 0


# The poller picks the cap as `queue_max_limit or plan_cap` — so uncapped queues
# (max-limit 0/0, common on unlimited plans) fall back to the plan rate.


def test_uncapped_queue_falls_back_to_plan_cap():
    queue_max = 0  # RouterOS "0/0" — unlimited queue
    plan_cap = 100_000_000  # plan provisions 100 Mbps
    # 491 Mbps glitch on an "unlimited" queue is still clamped to the plan rate.
    assert _clamp_rate(491_000_000, queue_max or plan_cap) == 100_000_000


def test_uncapped_queue_no_plan_known_passes_through():
    # No queue cap and no plan cap -> nothing to clamp against.
    assert _clamp_rate(491_000_000, 0 or 0) == 491_000_000
