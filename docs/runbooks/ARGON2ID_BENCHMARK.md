# Argon2id benchmark harness (source only)

Owner: Sub auth. This standalone evidence tool does not change application
hashing, authentication, credential state, limiter policy, or production parameters.
The existing auth owners retain those contracts; no service/registry owner is added.
No benchmark dispatch or environment run is authorized by this document.

From the repository root, a plan-only command (no hashing or cgroup reads):

```sh
python -m scripts.security.argon2id_benchmark
```

An operator separately authorized to run synthetic microbenchmarks can use:

```sh
python -m scripts.security.argon2id_benchmark --mode microbench --execute \
  --samples 30 --warmups 2 --max-seconds 120 > argon2id-single-op.json
```

Do not run this command during source review. Linux GitHub runner dispatch and
Seabone staging execution each require Michael's approval. No workflow auto-runs
benchmarks on pull requests. The harness needs only the already locked
`argon2-cffi` dependency; it imports no app, DB, settings, network or secret loader.
Use the repository's locked environment. The report records Python, OS, machine
architecture and both Argon2 package versions, avoiding hostnames and account IDs.

The default grid is m={19,32,46,64} MiB × t={2,3}, p=1. Explicit
`--include-p2-control` adds only m=64MiB,t=2,p=2. Argon2id v19 uses 32-byte
hashes and 16-byte random salts. Inputs are generated synthetic values in memory;
passwords and encoded hashes never appear in reports. One hash then one successful
verify runs serially per iteration. Warmups are excluded; p50/p95 use nearest rank.
This is neither request latency nor a concurrency/sustainable-throughput result.

Limits: 1..100 measured samples per point, 0..5 warmups, 1..120 seconds per run,
at most 9 points and one active operation (64MiB Argon2 memory plus process/library
overhead). The deadline is checked between operations and at completion; it cannot
interrupt a native operation already running. An expired/failed run exits nonzero
without a complete JSON report. External operator time/resource limits remain
necessary on approved runners; this tool does not configure them.

`--cgroup-directory` selects the actual cgroup v2 directory containing the process.
Default `/sys/fs/cgroup` is appropriate only when the process/container namespace
places its cgroup there; verify this on the approved runner. Before/after snapshots
read `memory.peak` and `cpu.stat` without writes/resets. A missing/unreadable counter
is null/unavailable, malformed data is null/malformed, and zero is available.
`memory.peak` is the selected cgroup's lifetime peak, not an isolated per-run peak.
CPU counters are cumulative snapshots; subtract matching before/after counters
for a run delta. Neither snapshot proves container isolation or production parity.

`OperationInterval` and `analyze_intervals` provide a benchmark-only analysis helper
for a future wrapper: `(pid,tid,seq,kind,t_start_ns,t_end_ns)`, kind one of
verify/hash/rehash. Merge intervals from processes on the **same host** using a
shared monotonic nanosecond clock. Use a sequence unique per `(pid,tid)` for the
whole capture (avoid PID reuse/capture mixing). The helper rejects malformed,
zero/negative-duration and duplicate identities, caps analysis at 100000 records,
and counts half-open intervals with end events before starts at ties. Separate
verify and subsequent rehash are sequential intervals. No Prometheus gauge or
sum of worker RSS substitutes for this overlap count. The helper does not collect
intervals, install application instrumentation, parse arbitrary JSONL, or export
process identifiers in the microbench artifact.

## Remaining C1–C6 acceptance work

The canonical security packet's C1–C6 still requires a separately reviewed workload
adapter and separately authorized measurements:

- C1: production-class Linux single-op evidence; approved read-only production CPU,
  worker/threadpool/Celery sizing lookup; staging under matching 4 CPU/8GiB limits
  and worker settings. Mac measurements are indicative and cannot substitute.
- C3: open-loop constant arrivals at {1,5,10,20,40}/s without coordinated omission;
  60% current verify/20% legacy verify+rehash/20% wrong password; 100% rehash storm;
  concurrent authenticated cheap-route canary; bursts {6,12,24,48}. None is built
  by this serial microbenchmark tool. No accounts/HTTP login adapter is included.
- C4: client login/canary p50/p95/p99, error/503 rate; benchmark-only per-process
  tmpfs interval wrapper and merge; worker VmHWM/PSS from smaps_rollup; per-core
  mpstat; dedicated container peak memory and throttling deltas. Only single-op
  p50/p95, the interval analysis helper, and selected cgroup snapshots exist here.
- C5: choose/test any per-process limiter separately, including timeout/503 and
  threadpool reserve, Celery parity and memory budget. No limiter is installed.
- C6: assemble each grid point's production-class single-op p95, storm p99, canary
  degradation, peak memory, throttling and sustainable logins/s at chosen k for
  Michael's decision. This harness makes no production parameter recommendation.

No SSH, Docker, staging, production, CI dispatch, real hashing workload or load run
is part of implementing/testing this source slice. Focused unit tests inject a
fake hasher and deterministic clock; they are correctness evidence only.
