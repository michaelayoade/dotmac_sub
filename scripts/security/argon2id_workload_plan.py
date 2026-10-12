"""Pure C3/C5 workload planning; no executor, account operations or measurements."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, localcontext
from enum import Enum

from scripts.security.argon2id_benchmark import Parameters

NANOSECONDS = 1_000_000_000
MAX_SCHEDULED_ARRIVALS = 100_000
MAX_TIME_NS = 2**63 - 1


class PlanError(ValueError):
    """Invalid source-only workload configuration (never an execution failure)."""

    code = "invalid_workload_plan"


class Scenario(str, Enum):
    MIXED = "mixed"
    REHASH_STORM = "rehash_storm"


class ArrivalKind(str, Enum):
    CONSTANT = "constant"
    BURST = "burst"


class AttemptKind(str, Enum):
    CURRENT_VERIFY = "current_verify"
    LEGACY_VERIFY_REHASH = "legacy_verify_rehash"
    WRONG_PASSWORD = "wrong_password"
    CANARY = "canary"


class FixturePolicy(str, Enum):
    ONE_USE_PER_ATTEMPT = "one_use_per_attempt"


MIXED_CYCLE = (
    AttemptKind.CURRENT_VERIFY,
    AttemptKind.LEGACY_VERIFY_REHASH,
    AttemptKind.CURRENT_VERIFY,
    AttemptKind.WRONG_PASSWORD,
    AttemptKind.CURRENT_VERIFY,
)


def _integer(
    name: str, value: int, *, minimum: int, maximum: int | None = None
) -> None:
    if type(value) is not int or value < minimum:
        raise PlanError(f"{name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise PlanError(f"{name} must be <= {maximum}")


def seconds_to_ns(value: str) -> int:
    """CLI adapter: require finite positive seconds exactly representable in ns."""
    try:
        seconds = Decimal(value)
    except InvalidOperation as exc:
        raise PlanError("seconds must be numeric") from exc
    if not seconds.is_finite() or seconds <= 0:
        raise PlanError("seconds must be finite and positive")
    if seconds < Decimal(1) / NANOSECONDS:
        raise PlanError("seconds must be exactly representable in nanoseconds")
    if seconds > Decimal(MAX_TIME_NS) / NANOSECONDS:
        raise PlanError("seconds exceed signed 64-bit nanosecond representation")
    with localcontext() as context:
        context.prec = max(30, len(seconds.as_tuple().digits) + 10)
        nanoseconds = seconds * NANOSECONDS
    if nanoseconds != nanoseconds.to_integral_value():
        raise PlanError("seconds must be exactly representable in nanoseconds")
    return int(nanoseconds)


@dataclass(frozen=True)
class GridPoint:
    parameters: Parameters

    def __post_init__(self) -> None:
        if (
            not isinstance(self.parameters, Parameters)
            or self.parameters.parallelism != 1
        ):
            raise PlanError("workload grid requires existing Parameters with p=1")


@dataclass(frozen=True)
class ArrivalPlan:
    kind: ArrivalKind
    rate_per_second: int | None
    burst_size: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ArrivalKind):
            raise PlanError("arrival kind must be typed")
        if self.kind is ArrivalKind.CONSTANT:
            if type(self.rate_per_second) is not int or self.rate_per_second not in (
                1,
                5,
                10,
                20,
                40,
            ):
                raise PlanError("constant rate must be 1, 5, 10, 20 or 40/s")
            if self.burst_size is not None:
                raise PlanError("constant arrivals cannot also specify a burst")
        else:
            if type(self.burst_size) is not int or self.burst_size not in (
                6,
                12,
                24,
                48,
            ):
                raise PlanError("burst size must be 6, 12, 24 or 48")
            if self.rate_per_second is not None:
                raise PlanError("burst arrivals cannot also specify a rate")


@dataclass(frozen=True)
class RunLimits:
    point_duration_ns: int
    canary_cadence_ns: int
    request_timeout_ns: int
    max_inflight: int
    candidate_k_per_process: int
    waiter_bound_per_process: int
    threadpool_total_per_process: int
    thread_reserve_per_process: int

    def __post_init__(self) -> None:
        _integer(
            "point duration ns",
            self.point_duration_ns,
            minimum=1,
            maximum=600 * NANOSECONDS,
        )
        _integer(
            "canary cadence ns",
            self.canary_cadence_ns,
            minimum=1,
            maximum=self.point_duration_ns,
        )
        _integer(
            "request timeout ns",
            self.request_timeout_ns,
            minimum=1,
            maximum=MAX_TIME_NS,
        )
        _integer("global max inflight", self.max_inflight, minimum=1, maximum=48)
        _integer(
            "candidate k",
            self.candidate_k_per_process,
            minimum=1,
            maximum=self.max_inflight,
        )
        _integer(
            "waiter bound",
            self.waiter_bound_per_process,
            minimum=0,
            maximum=self.max_inflight,
        )
        _integer("threadpool total", self.threadpool_total_per_process, minimum=1)
        _integer("thread reserve", self.thread_reserve_per_process, minimum=1)
        if (
            self.candidate_k_per_process
            + self.waiter_bound_per_process
            + self.thread_reserve_per_process
            > self.threadpool_total_per_process
        ):
            raise PlanError(
                "candidate k plus waiters must leave the explicit thread reserve"
            )
        if (
            self.candidate_k_per_process + self.waiter_bound_per_process
            > self.max_inflight
        ):
            raise PlanError(
                "per-process active plus waiters exceeds global inflight cap"
            )


@dataclass(frozen=True)
class AttemptCounts:
    current_verify: int
    legacy_verify_rehash: int
    wrong_password: int

    @property
    def total(self) -> int:
        return self.current_verify + self.legacy_verify_rehash + self.wrong_password


@dataclass(frozen=True)
class FixtureDemand:
    current_verify_one_use: int
    legacy_verify_rehash_one_use: int
    wrong_password_one_use: int
    current_policy: FixturePolicy
    legacy_policy: FixturePolicy
    wrong_password_policy: FixturePolicy
    authenticated_canary_sessions: int


@dataclass(frozen=True)
class ScheduledArrival:
    scheduled_offset_ns: int
    kind: AttemptKind


@dataclass(frozen=True)
class WorkloadReport:
    plan: WorkloadPlan
    intended_login_attempts: int
    attempt_counts: AttemptCounts
    intended_canary_attempts: int
    fixture_demand: FixtureDemand
    mixed_cycle: tuple[AttemptKind, ...]
    timing_contract: str
    inflight_contract: str
    disposition: str
    abort_policy_status: str


@dataclass(frozen=True)
class WorkloadPlan:
    point: GridPoint
    arrivals: ArrivalPlan
    scenario: Scenario
    limits: RunLimits

    def __post_init__(self) -> None:
        if not isinstance(self.point, GridPoint) or not isinstance(
            self.arrivals, ArrivalPlan
        ):
            raise PlanError("point and arrivals must use typed contracts")
        if not isinstance(self.scenario, Scenario) or not isinstance(
            self.limits, RunLimits
        ):
            raise PlanError("scenario and limits must use typed contracts")
        if (
            self.intended_login_attempts + self.intended_canary_attempts
            > MAX_SCHEDULED_ARRIVALS
        ):
            raise PlanError(
                "at most 100000 scheduled login plus canary arrivals per plan"
            )

    @property
    def intended_login_attempts(self) -> int:
        if self.arrivals.kind is ArrivalKind.BURST:
            assert self.arrivals.burst_size is not None
            return self.arrivals.burst_size
        assert self.arrivals.rate_per_second is not None
        interval = NANOSECONDS // self.arrivals.rate_per_second
        return (self.limits.point_duration_ns + interval - 1) // interval

    @property
    def intended_canary_attempts(self) -> int:
        duration = self.limits.point_duration_ns
        cadence = self.limits.canary_cadence_ns
        return (duration + cadence - 1) // cadence

    def attempt_kind(self, index: int) -> AttemptKind:
        _integer(
            "attempt index", index, minimum=0, maximum=self.intended_login_attempts - 1
        )
        if self.scenario is Scenario.REHASH_STORM:
            return AttemptKind.LEGACY_VERIFY_REHASH
        return MIXED_CYCLE[index % len(MIXED_CYCLE)]

    @property
    def attempt_counts(self) -> AttemptCounts:
        total = self.intended_login_attempts
        if self.scenario is Scenario.REHASH_STORM:
            return AttemptCounts(0, total, 0)
        cycles, tail = divmod(total, len(MIXED_CYCLE))
        prefix = MIXED_CYCLE[:tail]
        return AttemptCounts(
            cycles * 3 + prefix.count(AttemptKind.CURRENT_VERIFY),
            cycles + prefix.count(AttemptKind.LEGACY_VERIFY_REHASH),
            cycles + prefix.count(AttemptKind.WRONG_PASSWORD),
        )

    @property
    def fixture_demand(self) -> FixtureDemand:
        counts = self.attempt_counts
        return FixtureDemand(
            current_verify_one_use=counts.current_verify,
            legacy_verify_rehash_one_use=counts.legacy_verify_rehash,
            wrong_password_one_use=counts.wrong_password,
            current_policy=FixturePolicy.ONE_USE_PER_ATTEMPT,
            legacy_policy=FixturePolicy.ONE_USE_PER_ATTEMPT,
            wrong_password_policy=FixturePolicy.ONE_USE_PER_ATTEMPT,
            authenticated_canary_sessions=1,
        )

    def scheduled_arrivals(self) -> Iterator[ScheduledArrival]:
        """Enumerate intended offsets only; no clock, sleeps, callbacks or dispatch.

        Login before canary at ties; burst logins retain cycle order at time zero.
        Completion times cannot alter this schedule. Future executors must record
        missed/rejected arrivals instead of shifting them to completion times.
        """
        login_index = canary_index = 0
        rate = self.arrivals.rate_per_second
        interval = NANOSECONDS // rate if rate is not None else 0
        while (
            login_index < self.intended_login_attempts
            or canary_index < self.intended_canary_attempts
        ):
            login_at = login_index * interval
            canary_at = canary_index * self.limits.canary_cadence_ns
            if login_index < self.intended_login_attempts and (
                canary_index >= self.intended_canary_attempts or login_at <= canary_at
            ):
                yield ScheduledArrival(login_at, self.attempt_kind(login_index))
                login_index += 1
            else:
                yield ScheduledArrival(canary_at, AttemptKind.CANARY)
                canary_index += 1

    def report(self) -> WorkloadReport:
        return WorkloadReport(
            plan=self,
            intended_login_attempts=self.intended_login_attempts,
            attempt_counts=self.attempt_counts,
            intended_canary_attempts=self.intended_canary_attempts,
            fixture_demand=self.fixture_demand,
            mixed_cycle=MIXED_CYCLE if self.scenario is Scenario.MIXED else (),
            timing_contract="Offsets from scheduled start in [0,point_duration_ns); constant arrivals start at zero, burst at zero; canary starts at zero; login before canary at ties; no response-completion rescheduling. Timeout is measured from each scheduled arrival; no late admission after point deadline. All intended arrivals remain counted even if later missed/rejected.",
            inflight_contract="Global max_inflight includes login and canary outstanding requests across all processes; k/waiters/pool/reserve are explicit per-process candidates. A future executor must enforce the global cap and record rejected/missed arrivals without dropping them from denominators; no limiter is installed.",
            disposition="SOURCE-ONLY PLAN: no execution, no acceptance, no approval, no performance predictions; no selected production parameters.",
            abort_policy_status="Deferred to a separately reviewed execution packet: typed resource identities, sampling/windows, error and health classification, and throttle numerator/denominator definitions are required before any abort policy is executable. No abort policy is implemented or accepted by this plan.",
        )

    def to_json(self) -> str:
        return json.dumps(
            asdict(self.report()), indent=2, sort_keys=True, allow_nan=False
        )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI adapter; validated explicit inputs produce only configuration/counts JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--memory-mib", type=int, choices=(19, 32, 46, 64), required=True
    )
    parser.add_argument("--time-cost", type=int, choices=(2, 3), required=True)
    parser.add_argument(
        "--scenario", choices=[v.value for v in Scenario], required=True
    )
    arrival = parser.add_mutually_exclusive_group(required=True)
    arrival.add_argument("--rate-per-second", type=int, choices=(1, 5, 10, 20, 40))
    arrival.add_argument("--burst-size", type=int, choices=(6, 12, 24, 48))
    for name in (
        "point-duration-seconds",
        "canary-cadence-seconds",
        "request-timeout-seconds",
    ):
        parser.add_argument(f"--{name}", required=True)
    for name in (
        "max-inflight",
        "candidate-k-per-process",
        "waiter-bound-per-process",
        "threadpool-total-per-process",
        "thread-reserve-per-process",
    ):
        parser.add_argument(f"--{name}", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        plan = WorkloadPlan(
            point=GridPoint(Parameters(args.memory_mib, args.time_cost)),
            arrivals=ArrivalPlan(
                kind=ArrivalKind.CONSTANT
                if args.rate_per_second is not None
                else ArrivalKind.BURST,
                rate_per_second=args.rate_per_second,
                burst_size=args.burst_size,
            ),
            scenario=Scenario(args.scenario),
            limits=RunLimits(
                point_duration_ns=seconds_to_ns(args.point_duration_seconds),
                canary_cadence_ns=seconds_to_ns(args.canary_cadence_seconds),
                request_timeout_ns=seconds_to_ns(args.request_timeout_seconds),
                max_inflight=args.max_inflight,
                candidate_k_per_process=args.candidate_k_per_process,
                waiter_bound_per_process=args.waiter_bound_per_process,
                threadpool_total_per_process=args.threadpool_total_per_process,
                thread_reserve_per_process=args.thread_reserve_per_process,
            ),
        )
    except PlanError as exc:
        parser.error(f"{exc.code}: {exc}")
    print(plan.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
