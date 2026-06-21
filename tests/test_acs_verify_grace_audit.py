"""Audit/dry-run classifier for the ACS verify-read grace (review #19, PR1).

This is observability only — it classifies a post-apply verify mismatch as
ACS-inform-lag candidate vs genuine divergence so we can measure
`would_be_graced` before any enforcement change. No reconcile behavior changes.
"""

from __future__ import annotations

from app.services.network.reconcile import AppliedAction, Drift
from app.services.network.reconcile.core import _classify_verify_drifts


def _drift(field, surface, repairable=True):
    return Drift(
        field=field, surface=surface, desired="x", observed="y", repairable=repairable
    )


def _applied(field, surface):
    return AppliedAction(
        field=field, surface=surface, old_value="y", new_value="x", duration_ms=1
    )


def test_acs_field_just_written_is_cache_lag_candidate():
    drifts = [_drift("pppoe_username", "acs")]
    applied = [_applied("pppoe_username", "acs")]
    cache_lag, genuine = _classify_verify_drifts(drifts, applied)
    assert cache_lag == drifts
    assert genuine == []
    assert not genuine  # would_be_graced


def test_olt_surface_drift_is_genuine():
    drifts = [_drift("line_profile", "olt")]
    applied = [_applied("line_profile", "olt")]
    cache_lag, genuine = _classify_verify_drifts(drifts, applied)
    assert cache_lag == []
    assert genuine == drifts  # OLT reads immediately; not cache lag


def test_acs_field_not_written_is_genuine():
    drifts = [_drift("wifi_ssid", "acs")]
    applied = [_applied("pppoe_username", "acs")]  # different field
    cache_lag, genuine = _classify_verify_drifts(drifts, applied)
    assert genuine == drifts


def test_unrepairable_acs_field_is_genuine():
    drifts = [_drift("wifi_password", "acs", repairable=False)]
    applied = [_applied("wifi_password", "acs")]
    cache_lag, genuine = _classify_verify_drifts(drifts, applied)
    assert genuine == drifts


def test_mixed_drift_not_fully_graced():
    drifts = [_drift("pppoe_username", "acs"), _drift("line_profile", "olt")]
    applied = [_applied("pppoe_username", "acs"), _applied("line_profile", "olt")]
    cache_lag, genuine = _classify_verify_drifts(drifts, applied)
    assert len(cache_lag) == 1  # the acs one
    assert len(genuine) == 1  # the olt one
    assert genuine  # NOT would_be_graced — a genuine drift remains
