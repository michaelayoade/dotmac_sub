# Argon2id workload planner (source only)

Owner: Sub auth, alongside [the benchmark contract](ARGON2ID_BENCHMARK.md).
The planner owns a typed description of intended workload only. Existing auth,
credential, limiter and transaction owners retain their contracts; no registry
service or production default is added. This document authorizes no environment run.

`scripts/security/argon2id_workload_plan.py` produces JSON configuration, counts
and timing semantics. It has no executor, `--execute` option, HTTP client,
hasher, account operations, app/DB/settings/secret-loader imports, environment
reads, or import-time dispatch. It reuses the existing benchmark `Parameters`
value object; importing it does not run that module's microbenchmark. No passwords,
encoded hashes, fixture identifiers, real accounts, host identity or request
payloads are generated or serialized.

Run `python -m scripts.security.argon2id_workload_plan --help` to inspect the CLI.
Each invocation requires an explicitly chosen single point and all these inputs:

| CLI input | Contract |
| --- | --- |
| `--memory-mib`, `--time-cost` | Existing m={19,32,46,64} MiB × t={2,3}, p=1; no p=2 control |
| `--scenario` | `mixed` or `rehash_storm` |
| `--rate-per-second` or `--burst-size` | Exactly one: {1,5,10,20,40}/s or {6,12,24,48} |
| `--point-duration-seconds` | Required, positive, at most 600 seconds |
| `--canary-cadence-seconds` | Required, positive and at most point duration |
| `--request-timeout-seconds` | Required, positive; starts at scheduled arrival |
| `--max-inflight` | Required global cap, 1..48, including canaries across processes |
| `--candidate-k-per-process` | Required positive candidate, at most global cap |
| `--waiter-bound-per-process` | Required nonnegative bound; k + waiters at most global cap |
| `--threadpool-total-per-process`, `--thread-reserve-per-process` | Required positive values; k + waiters + reserve at most total |

Seconds must be finite and exactly representable as positive signed 64-bit integer
nanoseconds. Invalid configuration exits nonzero without a plan JSON report.
No input selects production parameters, and this document supplies no candidate
k, waiter, pool, reserve, cadence, timeout or run-duration recommendation. Planning
one point does not establish completion of the full grid/scenario/rate/burst matrix.

Waiters consume caller threads, matching `NativePasswordAdmission`: the candidate
k plus maximum waiters must fit within total threadpool tokens minus reserved
tokens. The reserve remains available to unrelated work even when all candidate
slots and waiter slots are occupied.

`GridPoint`, `ArrivalPlan`, `Scenario`, `RunLimits` and `WorkloadPlan` are immutable
typed input boundaries. `AttemptCounts`, `FixtureDemand`, `ScheduledArrival` and
`WorkloadReport` are typed derived results. `PlanError` has stable code
`invalid_workload_plan`. Tests construct these same contracts.

## Intended timing and counts

The scheduling interval is half-open `[0, point_duration_ns)`. Constant arrivals
use offset `i * (1_000_000_000 / rate)` starting at zero; all allowed rates divide
the nanosecond second exactly. Bursts schedule their complete size at zero.
Canaries start at zero and recur independently at the explicit cadence before the
point deadline. At ties, login attempts precede the canary; tied burst attempts
retain cycle order. No actual clock is read. `scheduled_arrivals()` only enumerates
planned offsets, with no sleeps, callbacks, HTTP or completion inputs.

Total constant arrivals are `ceil(duration_ns / interval_ns)`; total canaries are
`ceil(duration_ns / cadence_ns)`. A structural planner bound rejects more than
100,000 combined scheduled login and canary arrivals before enumeration. The
iterator streams records without allocating the schedule. At the maximum rate
and duration there are 24,000 intended logins; the canary cadence must still fit
the combined record bound. This is a source-tool bound, not a capacity prediction.

Mixed attempts repeat the deterministic balanced cycle:
`current_verify, legacy_verify_rehash, current_verify, wrong_password, current_verify`.
Every full cycle is exactly 60%/20%/20%. A tail is the corresponding cycle prefix:
no attempts are rounded away, and each category's prefix count differs from its
ideal share by less than one. A six-attempt unit fixture plans four current, one
legacy+rehash and one wrong attempt. Storm attempts are all legacy-verify+rehash.
Canary attempts have their own count and do not change the login scenario's
composition or denominator.

A future executor must drive arrivals from these scheduled times, record lateness
and every missed/rejected arrival, and measure timeout from scheduled arrival.
It must never shift new arrivals to previous response completion or admit a late
arrival after the point deadline. An admitted request can remain outstanding up
to its timeout beyond that deadline; this planner does not run or drain it.
The global inflight cap includes login and canary requests. A burst plus a tied
canary can exceed that cap in intended demand; future admission must preserve
both intended counts and rejection/miss evidence. The planner installs no limiter
and predicts neither actual concurrency nor admission outcomes.

## Fixture demand only

Fixture pools are disjoint. `FixtureDemand` requires one-use fixtures for every
current verify, every designated legacy-verify+rehash, and every wrong-password
attempt; plus one independently authenticated cheap-route canary session.
These are allocation counts, not credentials or account identities. Creating,
authenticating, restoring or cleaning up fixtures is outside this planner.

Each designated rehash attempt requires a fresh eligible legacy credential. After
its first successful upgrade that fixture is no longer legacy: reusing it would
invalidate the rehash-storm design. Wrong-password attempts require their own
one-use targets, isolated from valid and legacy targets, to avoid per-account
lockout contamination. No reset/unlock/account mutation is performed here. Shared
rate-limit effects and expected authentication failures still need explicit
classification in the execution packet; fixture separation is not a claim that
those controls are bypassed or measured.

## Remaining separately reviewed execution work

Every JSON report labels **no execution, no acceptance, no approval, no performance
predictions** and no selected production parameters. Abort policies are explicitly
deferred; the planner reports no purportedly executable threshold policy.

Before execution, C3–C6 still need the separately reviewed and authorized workload
adapter, fixture lifecycle, global admission enforcement, real client login/canary
timings, interval collection and merging, resource measurements, and the canonical
acceptance decision. The staged abort requirements (85% memory limit, any OOM,
over 5% unexpected errors, health failure, and over 50% throttling over 60 seconds)
need a typed execution packet defining exact resource identities, sample clocks,
windows, minimum observations, error numerator/denominator and expected-failure
classification, health signals, throttle numerator/denominator and action ownership.
This source slice does not invent those definitions or accept their absence.

Focused fake-plan tests verify deterministic timing, ties, bursts, balanced tails,
fixture conservation, bounds and CLI validation. They provide source correctness
evidence only; they run no native hashing, HTTP load, Docker, SSH, staging,
production or CI dispatch. No Linux acceptance, chosen production parameters,
request latency or sustainable throughput follows from these tests.
