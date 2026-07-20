"""Tests for the reconciler alert-escalation module.

Covers:
* escalate_sweep_unreachable threshold-crossing semantics: only the cycle
  that crosses ``before < threshold <= after`` fires the ERROR log;
  subsequent unreachable cycles log at DEBUG.
* resolve_sweep_unreachable behavior (only fires when ``before > 0``).

(The Zabbix trapper push path was retired with the native monitoring
cutover; the structured log line is the only output path.)
"""

from __future__ import annotations

import logging

from app.services.network.reconcile.alerts import (
    DEFAULT_SWEEP_THRESHOLD,
    SWEEP_ALERT_KIND,
    default_threshold_from_env,
    escalate_sweep_unreachable,
    resolve_sweep_unreachable,
)

# ── default_threshold_from_env ─────────────────────────────────────────────


def test_default_threshold_from_env_default(monkeypatch):
    monkeypatch.delenv("RECONCILE_SWEEP_ALERT_THRESHOLD", raising=False)
    assert default_threshold_from_env() == DEFAULT_SWEEP_THRESHOLD


def test_default_threshold_from_env_override(monkeypatch):
    monkeypatch.setenv("RECONCILE_SWEEP_ALERT_THRESHOLD", "5")
    assert default_threshold_from_env() == 5


def test_default_threshold_from_env_invalid(monkeypatch):
    monkeypatch.setenv("RECONCILE_SWEEP_ALERT_THRESHOLD", "garbage")
    assert default_threshold_from_env() == DEFAULT_SWEEP_THRESHOLD


def test_default_threshold_from_env_zero_falls_back(monkeypatch):
    """Zero or negative threshold isn't valid; fall back to default rather
    than silently disable alerts."""
    monkeypatch.setenv("RECONCILE_SWEEP_ALERT_THRESHOLD", "0")
    assert default_threshold_from_env() == DEFAULT_SWEEP_THRESHOLD


# ── escalate_sweep_unreachable ─────────────────────────────────────────────


def test_escalate_emits_error_log_on_threshold_crossing(caplog):
    caplog.set_level(logging.ERROR)
    escalate_sweep_unreachable(
        ont_id="ont-1",
        serial_number="HWTC12345678",
        mgmt_ip="172.16.210.20",
        before=2,
        after=3,
        threshold=3,
    )
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    rec = error_records[0]
    assert rec.message == SWEEP_ALERT_KIND
    assert getattr(rec, "alert_action", None) == "escalate"
    assert getattr(rec, "after", None) == 3


def test_escalate_does_not_re_alert_on_subsequent_cycles(caplog):
    """Once past the threshold, subsequent unreachable cycles don't
    re-emit at ERROR (would page the operator twice)."""
    caplog.set_level(logging.DEBUG)
    escalate_sweep_unreachable(
        ont_id="ont-1",
        serial_number="HWTC12345678",
        mgmt_ip="172.16.210.20",
        before=5,
        after=6,
        threshold=3,
    )
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records == []
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(r.message == SWEEP_ALERT_KIND for r in debug_records)


def test_escalate_does_not_alert_before_threshold(caplog):
    """before=0, after=1 with threshold=3 → not crossing yet, log at
    DEBUG only."""
    caplog.set_level(logging.DEBUG)
    escalate_sweep_unreachable(
        ont_id="ont-1",
        serial_number="x",
        mgmt_ip=None,
        before=0,
        after=1,
        threshold=3,
    )
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records == []


# ── resolve_sweep_unreachable ──────────────────────────────────────────────


def test_resolve_fires_only_when_recovering_from_nonzero_counter(caplog):
    """resolve_sweep_unreachable is a no-op when before == 0 — successful
    reconciles on already-healthy ONTs don't spam recovery alerts."""
    caplog.set_level(logging.INFO)
    resolve_sweep_unreachable(
        ont_id="ont-1",
        serial_number="x",
        mgmt_ip="172.16.210.20",
        before=0,
    )
    assert [r for r in caplog.records if r.levelno == logging.INFO] == []


def test_resolve_emits_info_log_when_recovering(caplog):
    caplog.set_level(logging.INFO)
    resolve_sweep_unreachable(
        ont_id="ont-1",
        serial_number="x",
        mgmt_ip="172.16.210.20",
        before=5,
    )
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    assert getattr(info_records[0], "alert_action", None) == "resolved"
