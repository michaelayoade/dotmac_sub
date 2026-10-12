"""Pure fake-plan unit evidence; no account, hashing or HTTP workload."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from scripts.security import argon2id_workload_plan as planner
from scripts.security.argon2id_benchmark import Parameters


def limits() -> planner.RunLimits:
    return planner.RunLimits(
        point_duration_ns=planner.NANOSECONDS,
        canary_cadence_ns=planner.NANOSECONDS,
        request_timeout_ns=2 * planner.NANOSECONDS,
        max_inflight=48,
        candidate_k_per_process=2,
        waiter_bound_per_process=4,
        threadpool_total_per_process=8,
        thread_reserve_per_process=2,
    )


def plan(
    *,
    rate: int | None = 10,
    burst: int | None = None,
    scenario: planner.Scenario = planner.Scenario.MIXED,
    run_limits: planner.RunLimits | None = None,
) -> planner.WorkloadPlan:
    return planner.WorkloadPlan(
        point=planner.GridPoint(Parameters(19, 2)),
        arrivals=planner.ArrivalPlan(
            kind=planner.ArrivalKind.BURST
            if burst is not None
            else planner.ArrivalKind.CONSTANT,
            rate_per_second=rate,
            burst_size=burst,
        ),
        scenario=scenario,
        limits=run_limits if run_limits is not None else limits(),
    )


@pytest.mark.parametrize("rate", [1, 5, 10, 20, 40])
def test_constant_schedule_is_open_loop_and_half_open(rate: int) -> None:
    workload = plan(rate=rate)
    arrivals = tuple(workload.scheduled_arrivals())
    logins = [row for row in arrivals if row.kind is not planner.AttemptKind.CANARY]
    assert len(logins) == workload.intended_login_attempts == rate
    assert [row.scheduled_offset_ns for row in logins] == [
        i * planner.NANOSECONDS // rate for i in range(rate)
    ]
    assert all(row.scheduled_offset_ns < planner.NANOSECONDS for row in arrivals)
    assert workload.attempt_counts.total == rate
    assert tuple(workload.scheduled_arrivals()) == arrivals


def test_schedule_ties_login_before_canary_and_keeps_canary_independent() -> None:
    workload = plan(rate=5, run_limits=replace(limits(), canary_cadence_ns=200_000_000))
    rows = tuple(workload.scheduled_arrivals())
    assert len(rows) == 10
    for index in range(5):
        assert rows[index * 2].kind is workload.attempt_kind(index)
        assert rows[index * 2 + 1].kind is planner.AttemptKind.CANARY
        assert (
            rows[index * 2].scheduled_offset_ns
            == rows[index * 2 + 1].scheduled_offset_ns
        )


def test_fractional_duration_keeps_every_arrival_before_deadline() -> None:
    workload = plan(
        rate=5, run_limits=replace(limits(), point_duration_ns=1_050_000_000)
    )
    assert workload.intended_login_attempts == 6
    assert workload.intended_canary_attempts == 2
    assert workload.attempt_counts == planner.AttemptCounts(4, 1, 1)
    assert (
        max(row.scheduled_offset_ns for row in workload.scheduled_arrivals())
        == planner.NANOSECONDS
    )


@pytest.mark.parametrize("burst", [6, 12, 24, 48])
def test_burst_counts_conserve_tails_and_allocate_one_use_fixtures(burst: int) -> None:
    workload = plan(rate=None, burst=burst)
    rows = tuple(workload.scheduled_arrivals())
    assert len(rows) == burst + 1
    assert all(row.scheduled_offset_ns == 0 for row in rows)
    assert rows[-1].kind is planner.AttemptKind.CANARY
    counts = workload.attempt_counts
    assert counts.total == burst
    for kind, count in (
        (planner.AttemptKind.CURRENT_VERIFY, counts.current_verify),
        (planner.AttemptKind.LEGACY_VERIFY_REHASH, counts.legacy_verify_rehash),
        (planner.AttemptKind.WRONG_PASSWORD, counts.wrong_password),
    ):
        assert sum(row.kind is kind for row in rows) == count
    demand = workload.fixture_demand
    assert demand.legacy_verify_rehash_one_use == counts.legacy_verify_rehash
    assert demand.wrong_password_one_use == counts.wrong_password
    assert demand.current_verify_one_use == counts.current_verify
    assert demand.legacy_policy is planner.FixturePolicy.ONE_USE_PER_ATTEMPT
    assert demand.wrong_password_policy is planner.FixturePolicy.ONE_USE_PER_ATTEMPT


@pytest.mark.parametrize("total", range(1, 21))
def test_balanced_cycle_prefixes_preserve_exact_intended_total(total: int) -> None:
    workload = plan(
        rate=1,
        run_limits=replace(limits(), point_duration_ns=total * planner.NANOSECONDS),
    )
    counts = workload.attempt_counts
    assert counts.total == total
    assert abs(counts.current_verify - total * 0.6) < 1
    assert abs(counts.legacy_verify_rehash - total * 0.2) < 1
    assert abs(counts.wrong_password - total * 0.2) < 1
    if total % 5 == 0:
        assert counts == planner.AttemptCounts(total * 3 // 5, total // 5, total // 5)


def test_storm_requires_fresh_legacy_fixture_for_every_attempt() -> None:
    workload = plan(rate=None, burst=48, scenario=planner.Scenario.REHASH_STORM)
    assert workload.attempt_counts == planner.AttemptCounts(0, 48, 0)
    assert workload.fixture_demand.legacy_verify_rehash_one_use == 48
    assert workload.fixture_demand.wrong_password_one_use == 0
    assert all(
        workload.attempt_kind(i) is planner.AttemptKind.LEGACY_VERIFY_REHASH
        for i in range(48)
    )


@pytest.mark.parametrize("memory", [19, 32, 46, 64])
@pytest.mark.parametrize("time_cost", [2, 3])
def test_existing_grid_is_reused(memory: int, time_cost: int) -> None:
    assert planner.GridPoint(Parameters(memory, time_cost)).parameters == Parameters(
        memory, time_cost
    )


def test_p2_control_is_excluded_from_workload_grid() -> None:
    with pytest.raises(planner.PlanError, match="p=1"):
        planner.GridPoint(Parameters(64, 2, 2))


@pytest.mark.parametrize("rate", [0, 2, 41, True, 1.0])
def test_invalid_rates_rejected(rate: int) -> None:
    with pytest.raises(planner.PlanError):
        plan(rate=rate)


@pytest.mark.parametrize("burst", [0, 5, 49, True, 6.0])
def test_invalid_bursts_rejected(burst: int) -> None:
    with pytest.raises(planner.PlanError):
        plan(rate=None, burst=burst)


def test_ambiguous_arrival_and_untyped_scenario_rejected() -> None:
    with pytest.raises(planner.PlanError):
        plan(rate=5, burst=6)
    with pytest.raises(planner.PlanError):
        planner.ArrivalPlan(planner.ArrivalKind.CONSTANT, None, None)
    with pytest.raises(planner.PlanError):
        plan(scenario="mixed")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "duration", [0, -1, 600 * planner.NANOSECONDS + 1, True, float("nan"), float("inf")]
)
def test_duration_bounds(duration: int) -> None:
    with pytest.raises(planner.PlanError):
        replace(limits(), point_duration_ns=duration)


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf")])
def test_cadence_and_timeout_are_positive_typed_finite_ns(value: int) -> None:
    with pytest.raises(planner.PlanError):
        replace(limits(), canary_cadence_ns=value)
    with pytest.raises(planner.PlanError):
        replace(limits(), request_timeout_ns=value)


def test_capacity_bounds_and_thread_reserve() -> None:
    with pytest.raises(planner.PlanError):
        replace(limits(), max_inflight=49)
    with pytest.raises(planner.PlanError):
        replace(limits(), candidate_k_per_process=0)
    with pytest.raises(planner.PlanError):
        replace(limits(), waiter_bound_per_process=-1)
    with pytest.raises(planner.PlanError):
        replace(limits(), max_inflight=5)
    with pytest.raises(planner.PlanError):
        replace(limits(), thread_reserve_per_process=0)
    with pytest.raises(planner.PlanError):
        replace(limits(), threadpool_total_per_process=3)
    with pytest.raises(planner.PlanError):
        replace(limits(), canary_cadence_ns=planner.NANOSECONDS + 1)


def test_waiters_consume_caller_threads_and_preserve_exact_reserve_boundary() -> None:
    # k=2, waiters=4, total=8, reserve=2 exactly fits the admission owner contract.
    boundary = limits()
    assert boundary.candidate_k_per_process + boundary.waiter_bound_per_process == (
        boundary.threadpool_total_per_process - boundary.thread_reserve_per_process
    )
    with pytest.raises(planner.PlanError, match="k plus waiters"):
        replace(boundary, threadpool_total_per_process=7)
    with pytest.raises(planner.PlanError, match="k plus waiters"):
        replace(boundary, threadpool_total_per_process=4)


def test_schedule_record_cap_precedes_iteration() -> None:
    with pytest.raises(planner.PlanError, match="100000"):
        plan(run_limits=replace(limits(), canary_cadence_ns=1))
    boundary = plan(
        rate=40,
        run_limits=replace(
            limits(),
            point_duration_ns=600 * planner.NANOSECONDS,
            canary_cadence_ns=10_000_000,
        ),
    )
    assert boundary.intended_login_attempts == 24_000
    assert boundary.intended_canary_attempts == 60_000


@pytest.mark.parametrize(
    "value",
    [
        "nan",
        "NaN",
        "inf",
        "-inf",
        "0",
        "-1",
        "0.0000000001",
        "1e100000000",
        "bogus",
        "1e-100000000",
        "1.000000000000000000000000000000001",
    ],
)
def test_seconds_adapter_rejects_invalid_finite_and_precision_inputs(
    value: str,
) -> None:
    with pytest.raises(planner.PlanError):
        planner.seconds_to_ns(value)


def test_seconds_adapter_is_exact() -> None:
    assert planner.seconds_to_ns("0.000000001") == 1
    assert planner.seconds_to_ns("1.05") == 1_050_000_000


def test_serialized_plan_has_no_fixture_material_or_account_identifiers() -> None:
    report = json.loads(plan().to_json())

    def check_keys(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                assert key not in {
                    "password",
                    "hash",
                    "account_id",
                    "pid",
                    "hostname",
                    "token",
                    "url",
                }
                check_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                check_keys(nested)

    check_keys(report)


def cli_arguments() -> list[str]:
    return [
        "--memory-mib",
        "19",
        "--time-cost",
        "2",
        "--scenario",
        "mixed",
        "--burst-size",
        "6",
        "--point-duration-seconds",
        "1",
        "--canary-cadence-seconds",
        "1",
        "--request-timeout-seconds",
        "2",
        "--max-inflight",
        "48",
        "--candidate-k-per-process",
        "2",
        "--waiter-bound-per-process",
        "4",
        "--threadpool-total-per-process",
        "8",
        "--thread-reserve-per-process",
        "2",
    ]


def test_cli_only_emits_configuration_counts_and_timing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert planner.main(cli_arguments()) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["intended_login_attempts"] == 6
    assert report["attempt_counts"] == {
        "current_verify": 4,
        "legacy_verify_rehash": 1,
        "wrong_password": 1,
    }
    assert "no execution" in report["disposition"]
    assert "no acceptance" in report["disposition"]
    assert "no approval" in report["disposition"]
    assert "no performance predictions" in report["disposition"]
    assert "Deferred" in report["abort_policy_status"]
    assert report == json.loads(plan(rate=None, burst=6).to_json())


def test_cli_rejects_execute_and_requires_explicit_inputs() -> None:
    for args in (
        [],
        [*cli_arguments(), "--execute"],
        [*cli_arguments(), "--point-duration-seconds", "601"],
    ):
        with pytest.raises(SystemExit) as exc:
            planner.main(args)
        assert exc.value.code == 2


def test_invalid_attempt_index() -> None:
    for index in (-1, 10, True):
        with pytest.raises(planner.PlanError):
            plan().attempt_kind(index)
