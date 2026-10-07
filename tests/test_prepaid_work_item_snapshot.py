"""Prepaid finance work-item backlog and SLA signals.

Production 2026-10-06: every open prepaid work item showed the same
``sla_due_at`` because each scheduled run recomputed it. The deadline is now
fixed when an item opens (``admin_alerts.sync_alert``), and the enforcement
snapshot exports the open backlog and its overdue part so the
``PrepaidWorkItemsOverdue`` alert can fire.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.network_monitoring import AlertSeverity
from app.services import app_cache, observability
from app.services.collections import scheduled
from app.services.observability import Finding, record_finding, resolve_findings
from app.services.prepaid_renewal_terms_backfill import RENEWAL_TERMS_FINDING_PREFIX

_NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
_QUARANTINE = scheduled._QUARANTINE_FINDING_PREFIX


def _record(db, fingerprint: str, due_at: datetime) -> None:
    record_finding(
        db,
        Finding(
            fingerprint=fingerprint,
            domain="prepaid_enforcement",
            source="test",
            severity=AlertSeverity.warning,
            title="Prepaid work item",
            summary="Needs finance review.",
            details={"owner": "finance-billing", "sla_due_at": due_at.isoformat()},
        ),
    )


def test_work_item_counts_report_open_backlog_and_only_open_overdue_items(
    db_session,
):
    _record(db_session, f"{_QUARANTINE}q-overdue", _NOW - timedelta(hours=2))
    _record(db_session, f"{_QUARANTINE}q-due-later", _NOW + timedelta(hours=2))
    _record(
        db_session,
        f"{RENEWAL_TERMS_FINDING_PREFIX}r-overdue",
        _NOW - timedelta(days=60),
    )
    _record(
        db_session,
        f"{RENEWAL_TERMS_FINDING_PREFIX}r-resolved",
        _NOW - timedelta(days=60),
    )
    _record(db_session, "prepaid-balance:no-contact:x", _NOW - timedelta(days=60))
    db_session.commit()
    resolve_findings(
        db_session,
        managed_prefix=RENEWAL_TERMS_FINDING_PREFIX,
        active_fingerprints={f"{RENEWAL_TERMS_FINDING_PREFIX}r-overdue"},
    )
    db_session.commit()

    counts = scheduled._prepaid_work_item_counts(db_session, now=_NOW)

    assert counts == {
        "coverage_quarantine_work_items_open": 2.0,
        "renewal_terms_work_items_open": 1.0,
        "work_items_overdue": 2.0,
    }


def test_work_item_signals_fit_the_registered_snapshot_bound(monkeypatch):
    stored: dict[str, object] = {}
    monkeypatch.setattr(
        app_cache,
        "set_json",
        lambda key, payload, ttl: stored.update(payload=payload) or True,
    )

    scheduled._publish_prepaid_enforcement_snapshot(
        scheduled._REPAIR_FAILED,
        {"renewal_terms_unresolved": 7},
        {
            "coverage_quarantine_work_items_open": 9.0,
            "renewal_terms_work_items_open": 44.0,
            "work_items_overdue": 3.0,
        },
    )

    payload = stored["payload"]
    assert isinstance(payload, dict)
    observations = {item["signal"]: item["value"] for item in payload["observations"]}
    # The existing due-for-enforcement signal keeps its meaning.
    assert observations["renewal_terms_unresolved"] == 7.0
    assert observations["renewal_terms_work_items_open"] == 44.0
    assert observations["coverage_quarantine_work_items_open"] == 9.0
    assert observations["work_items_overdue"] == 3.0
    assert len(observations) <= int(
        observability._STATE_SNAPSHOT_SPECS["prepaid_enforcement"]["max_observations"]
    )


def test_failed_work_item_count_omits_signals_instead_of_publishing_zero(
    monkeypatch,
):
    stored: dict[str, object] = {}
    monkeypatch.setattr(
        app_cache,
        "set_json",
        lambda key, payload, ttl: stored.update(payload=payload) or True,
    )

    scheduled._publish_prepaid_enforcement_snapshot(scheduled._REPAIR_FAILED, {}, None)

    payload = stored["payload"]
    assert isinstance(payload, dict)
    signals = {item["signal"] for item in payload["observations"]}
    assert "work_items_overdue" not in signals
    assert "renewal_terms_work_items_open" not in signals
