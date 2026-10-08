"""Prepaid enforcement state signals represent a complete sweep cycle.

Production 2026-10-06..08: ``renewal_terms_unresolved`` swung 33 -> 30, 16,
10, 7, 4 during business hours while the open work-item backlog stayed flat
at 44. The bounded sweep resumes a keyset cycle across several
budget-limited runs, but each run published its own slice's counters as if
they were totals. The sweep now tallies per-account outcomes across the runs
of a cycle and the snapshot publishes only completed-cycle totals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.collections import PrepaidSweepCycleState
from app.models.subscriber import Subscriber
from app.services import app_cache
from app.services.collections import prepaid_balance_sweep as sweep
from app.services.collections import scheduled
from app.services.collections.prepaid_balance_sweep import (
    PrepaidSweepCycleTotals,
    PrepaidSweepOutcome,
    load_prepaid_sweep_cycle_totals,
    run_prepaid_balance_sweep,
)

_START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
_FAR = _START + timedelta(days=365)


class _Clock(datetime):
    """Deterministic ``datetime`` whose ``now`` advances per processed account."""

    current = _START

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        return cls.current


def _outcome_plan(count: int) -> list[str]:
    plan = []
    for index in range(count):
        if index % 3 == 0:
            plan.append(PrepaidSweepOutcome.renewal_terms_unresolved.value)
        elif index % 5 == 0:
            plan.append(PrepaidSweepOutcome.coverage_unresolved.value)
        elif index % 7 == 0:
            plan.append(PrepaidSweepOutcome.notice_suppressed.value)
        else:
            plan.append("ok")
    return plan


@pytest.fixture()
def cohort(db_session, monkeypatch):
    """Twelve candidate accounts with a fixed per-account planner outcome."""
    accounts = [
        Subscriber(first_name="Cycle", last_name=str(i), email=f"cycle{i}@ex.test")
        for i in range(12)
    ]
    db_session.add_all(accounts)
    db_session.query(PrepaidSweepCycleState).delete()
    db_session.commit()
    ids = sorted((account.id for account in accounts), key=str)
    outcomes = dict(zip(ids, _outcome_plan(len(ids)), strict=True))
    calls: list = []

    def _fake_process(db, account, now, cfg, **_kwargs):  # noqa: ANN001, ANN202
        calls.append(account.id)
        # Each account costs one minute of the run budget.
        _Clock.current = _Clock.current + timedelta(minutes=1)
        return outcomes[account.id]

    _Clock.current = _START
    monkeypatch.setattr(sweep, "datetime", _Clock)
    monkeypatch.setattr(sweep, "_process_account", _fake_process)
    monkeypatch.setattr(sweep, "_build_sweep_prefetch", lambda *a, **k: None)
    monkeypatch.setattr(sweep, "resolve_prepaid_enforcement_policy", lambda db: None)
    monkeypatch.setattr(sweep, "candidate_prepaid_account_ids", lambda db: list(ids))
    monkeypatch.setattr(
        sweep, "candidate_prepaid_funding_account_ids", lambda db: set()
    )
    monkeypatch.setattr(sweep, "prepaid_notice_suppression_reasons", lambda db, ids: {})
    monkeypatch.setattr(
        "app.services.prepaid_funding_reconstruction."
        "prepaid_funding_incomplete_source_account_ids",
        lambda db, ids: set(),
    )
    return {"ids": ids, "outcomes": outcomes, "calls": calls}


def _expected(outcomes: dict) -> dict[PrepaidSweepOutcome, int]:
    expected = dict.fromkeys(PrepaidSweepOutcome, 0)
    for value in outcomes.values():
        if value != "ok":
            expected[PrepaidSweepOutcome(value)] += 1
    return expected


def _run(db_session, *, budget_accounts: int | None):
    deadline = (
        _FAR
        if budget_accounts is None
        else _Clock.current + timedelta(minutes=budget_accounts)
    )
    return run_prepaid_balance_sweep(db_session, now=_Clock.current, deadline=deadline)


def test_single_full_run_publishes_complete_totals(db_session, cohort):
    assert load_prepaid_sweep_cycle_totals(db_session) is None

    result = _run(db_session, budget_accounts=None)

    assert result["accounts_processed"] == result["accounts_scanned"] == 12
    totals = load_prepaid_sweep_cycle_totals(db_session)
    assert totals is not None
    assert dict(totals.counts) == _expected(cohort["outcomes"])
    assert totals.count(PrepaidSweepOutcome.renewal_terms_unresolved) == 4


def test_cycle_split_across_budget_limited_runs_matches_single_full_run(
    db_session, cohort
):
    expected = _expected(cohort["outcomes"])
    runs = [_run(db_session, budget_accounts=5) for _ in range(3)]

    # 5 + 5 + 2: three bounded runs, each covering only a slice.
    assert [run["accounts_processed"] for run in runs] == [5, 5, 2]
    assert [run["cycle_remaining"] for run in runs] == [7, 2, 0]
    # The per-run slice counters are partial ...
    per_run = [int(run["renewal_terms_unresolved"]) for run in runs]
    assert sum(per_run) == expected[PrepaidSweepOutcome.renewal_terms_unresolved]
    assert all(value < sum(per_run) for value in per_run)
    # ... but the completed cycle equals a single full pass.
    totals = load_prepaid_sweep_cycle_totals(db_session)
    assert totals is not None
    assert dict(totals.counts) == expected
    assert sorted(cohort["calls"], key=str) == cohort["ids"]


def test_partial_runs_do_not_change_published_totals_until_cycle_completes(
    db_session, cohort
):
    _run(db_session, budget_accounts=None)
    first = load_prepaid_sweep_cycle_totals(db_session)
    assert first is not None

    # The world changes: every account now reports unresolved renewal terms.
    for key in cohort["outcomes"]:
        cohort["outcomes"][key] = PrepaidSweepOutcome.renewal_terms_unresolved.value

    _run(db_session, budget_accounts=4)
    _run(db_session, budget_accounts=4)
    mid = load_prepaid_sweep_cycle_totals(db_session)
    assert mid == first

    _run(db_session, budget_accounts=4)
    done = load_prepaid_sweep_cycle_totals(db_session)
    assert done is not None
    assert done.count(PrepaidSweepOutcome.renewal_terms_unresolved) == 12
    assert done.completed_at > first.completed_at


def test_crash_before_checkpoint_does_not_double_count(db_session, cohort, monkeypatch):
    expected = _expected(cohort["outcomes"])
    _run(db_session, budget_accounts=5)

    # The next run evaluates (and commits) accounts, then dies before its
    # cycle checkpoint commits: neither the cursor nor the tally advances.
    real_load = sweep._load_cycle_state
    loads = {"n": 0}

    def _crash_on_checkpoint(db):  # noqa: ANN001, ANN202
        loads["n"] += 1
        if loads["n"] == 2:
            raise RuntimeError("worker lost")
        return real_load(db)

    monkeypatch.setattr(sweep, "_load_cycle_state", _crash_on_checkpoint)
    crashed = _run(db_session, budget_accounts=5)
    assert crashed["accounts_processed"] == 5
    monkeypatch.setattr(sweep, "_load_cycle_state", real_load)

    # The retry re-evaluates the same accounts and finishes the cycle.
    _run(db_session, budget_accounts=None)
    totals = load_prepaid_sweep_cycle_totals(db_session)
    assert totals is not None
    assert dict(totals.counts) == expected


def test_reprocessed_account_is_tallied_once_with_latest_outcome():
    first = sweep._merge_cycle_outcomes(
        {}, {"a": "renewal_terms_unresolved", "b": "coverage_unresolved"}
    )
    again = sweep._merge_cycle_outcomes(
        first, {"a": "renewal_terms_unresolved", "b": "ok"}
    )
    assert again == {"a": "renewal_terms_unresolved"}


def test_cycle_started_before_tallying_is_never_published(db_session, cohort):
    _run(db_session, budget_accounts=5)
    state = db_session.query(PrepaidSweepCycleState).one()
    # Simulates the row as migrated by 651: in-progress cycle, NULL tally.
    state.cycle_outcomes = None
    db_session.commit()

    _run(db_session, budget_accounts=None)
    assert load_prepaid_sweep_cycle_totals(db_session) is None

    # The next fresh cycle is tallied from its start and published.
    _run(db_session, budget_accounts=None)
    totals = load_prepaid_sweep_cycle_totals(db_session)
    assert totals is not None
    assert dict(totals.counts) == _expected(cohort["outcomes"])


def _capture_snapshot(monkeypatch) -> dict[str, object]:
    stored: dict[str, object] = {}
    monkeypatch.setattr(
        app_cache,
        "set_json",
        lambda key, payload, ttl: stored.update(payload=payload) or True,
    )
    return stored


def _observations(stored: dict[str, object]) -> dict[str, float]:
    payload = stored["payload"]
    assert isinstance(payload, dict)
    return {item["signal"]: item["value"] for item in payload["observations"]}


def test_snapshot_publishes_cycle_totals_not_the_run_slice(monkeypatch):
    stored = _capture_snapshot(monkeypatch)
    completed = datetime(2026, 10, 7, 3, 0, tzinfo=UTC)
    totals = PrepaidSweepCycleTotals(
        completed_at=completed,
        counts={
            PrepaidSweepOutcome.renewal_terms_unresolved: 33,
            PrepaidSweepOutcome.coverage_unresolved: 2,
        },
    )

    scheduled._publish_prepaid_enforcement_snapshot(
        scheduled._REPAIR_FAILED,
        {
            "renewal_terms_unresolved": 7,
            "accounts_scanned": 3934,
            "accounts_processed": 800,
            "budget_deferred": 2133,
        },
        None,
        totals,
        now=completed + timedelta(hours=2),
    )

    observations = _observations(stored)
    assert observations["renewal_terms_unresolved"] == 33.0
    assert observations["coverage_unresolved"] == 2.0
    assert observations["no_contact_route"] == 0.0
    assert observations["cycle_totals_age_seconds"] == 7200.0
    assert observations["accounts_scanned"] == 3934.0
    assert observations["accounts_processed"] == 800.0


def test_snapshot_omits_state_signals_before_first_complete_cycle(monkeypatch):
    stored = _capture_snapshot(monkeypatch)

    scheduled._publish_prepaid_enforcement_snapshot(
        scheduled._REPAIR_FAILED, {"renewal_terms_unresolved": 7}, None, None
    )

    observations = _observations(stored)
    for name in (
        "renewal_terms_unresolved",
        "coverage_unresolved",
        "notice_suppressed",
        "no_contact_route",
        "delivery_unavailable",
        "cycle_totals_age_seconds",
    ):
        assert name not in observations
