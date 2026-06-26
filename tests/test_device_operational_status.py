"""Tests for derived device operational status (Phase 1).

See docs/designs/DEVICE_OPERATIONAL_STATUS.md.
"""

from types import SimpleNamespace

from app.services.device_operational_status import (
    DEGRADED,
    DOWN,
    MAINTENANCE,
    UNKNOWN,
    UNMONITORED,
    UP,
    annotate_operational_status,
    derive_operational_status,
)


class _Enum:
    """Mimics a SQLAlchemy enum value (has .value)."""

    def __init__(self, value):
        self.value = value


def _dev(status=None, live=None, enum=True):
    def wrap(v):
        if v is None:
            return None
        return _Enum(v) if enum else v

    return SimpleNamespace(status=wrap(status), live_status=wrap(live))


# ── precedence ladder ────────────────────────────────────────────────────────


def test_lifecycle_maintenance_overrides_observation():
    # even if live says down, an intentional maintenance state wins and is calm
    op = derive_operational_status(_dev("maintenance", "down"), warm_stale=False)
    assert op.status == MAINTENANCE
    assert op.alarming is False
    assert op.mismatch is False


def test_no_live_status_is_unmonitored_not_warmed():
    op = derive_operational_status(_dev("online", None), warm_stale=False)
    assert op.status == UNMONITORED
    assert op.reason == "not_warmed"
    assert op.alarming is False


def test_stale_warmer_is_unmonitored_even_when_live_up():
    op = derive_operational_status(_dev("online", "up"), warm_stale=True)
    assert op.status == UNMONITORED
    assert op.reason == "stale"


def test_live_unknown_is_unmonitored_not_down():
    op = derive_operational_status(_dev("online", "unknown"), warm_stale=False)
    assert op.status == UNMONITORED
    assert op.reason == "monitoring_unknown"


def test_live_problem_maps_to_degraded():
    op = derive_operational_status(_dev("online", "problem"), warm_stale=False)
    assert op.status == DEGRADED
    assert op.alarming is True


def test_live_down_maps_to_down():
    op = derive_operational_status(_dev("offline", "down"), warm_stale=False)
    assert op.status == DOWN
    assert op.alarming is True


def test_live_up_maps_to_up():
    op = derive_operational_status(_dev("online", "up"), warm_stale=False)
    assert op.status == UP
    assert op.alarming is False


def test_plain_string_attributes_supported():
    # device.status / live_status may already be plain strings on stub objects
    op = derive_operational_status(_dev("online", "up", enum=False), warm_stale=False)
    assert op.status == UP


def test_indeterminate_live_value_is_unknown():
    op = derive_operational_status(_dev("online", "weird"), warm_stale=False)
    assert op.status == UNKNOWN


# ── mismatch flags (inventory hygiene) ───────────────────────────────────────


def test_mismatch_admin_online_observed_down():
    op = derive_operational_status(_dev("online", "down"), warm_stale=False)
    assert op.mismatch is True
    assert op.mismatch_reason == "admin_online_observed_down"


def test_mismatch_admin_offline_observed_up():
    op = derive_operational_status(_dev("offline", "up"), warm_stale=False)
    assert op.mismatch is True
    assert op.mismatch_reason == "admin_offline_observed_up"


def test_mismatch_active_but_unmonitored():
    op = derive_operational_status(_dev("online", None), warm_stale=False)
    assert op.mismatch is True
    assert op.mismatch_reason == "active_but_unmonitored"


def test_no_mismatch_when_admin_agrees_with_observation():
    op = derive_operational_status(_dev("online", "up"), warm_stale=False)
    assert op.mismatch is False


# ── annotate helper ──────────────────────────────────────────────────────────


def test_annotate_sets_operational_attribute_and_is_render_safe():
    devices = [
        _dev("online", "up"),
        _dev("offline", "down"),
        SimpleNamespace(),  # missing attributes entirely -> must not raise
    ]
    annotate_operational_status(devices)
    assert devices[0].operational.status == UP
    assert devices[1].operational.status == DOWN
    # object with no status/live_status still gets a (safe) operational value
    assert devices[2].operational.status in (UNMONITORED, UNKNOWN)
