# Poller and syslog component probes

These read-only commands observe local runtime components. They do not authorize
any deployment or domain transition, constitute signed deployment-health evidence,
or replace authenticated application readiness. Root Compose is unchanged.

## Bandwidth poller loop liveness

```
python -m app.services.poller_health --max-age-seconds 60
```

Choose an explicit maximum age covering `BANDWIDTH_POLL_INTERVAL_MS` plus the
bounded worst complete cycle duration, including device concurrency and Redis
I/O. Sixty seconds is an example, not universal acceptance. The typed query
accepts positive finite budgets up to 3600 seconds. The incumbent writer and
`POLLER_HEALTH_KEY` remain the single projection; the command writes no heartbeat.
The Redis reader uses a 256 KiB bounded GETRANGE and two-second connect/read
socket timeouts. Missing configuration/key, oversized or malformed payload,
invalid counters/timestamps, Redis failure, future timestamps and expired evidence
make the CLI fail closed. The same validated `PollerSnapshot` feeds web metrics and NOC views;
existing metric names and NOC serialization remain intact. Legacy web display
freshness interpretation is preserved; the stricter future-timestamp rejection
belongs to the new liveness CLI, not a new web readiness gate.

Exit 0 means a fresh polling loop observation. It does **not** mean successful
collection: the writer publishes after a caught `_poll_once` failure too. Idle
on-demand cycles are live. Device failures are returned separately as
`devices_failing` and do not turn loop liveness into collection readiness.
No poller readiness command exists; readiness requires a future change to the
incumbent cycle owner that records actual successful-cycle provenance, including
sample/database/publication semantics. Do not map this command to a readiness
gate that requires successful processing.

## Syslog UDP receiver readiness

```
python -m app.syslog.health --timeout-seconds 2
```

The listener binds the real configured UDP address before opening a private
Unix stream health endpoint. It records monotonic progression only in its existing
receive-service loop, after its one-second await. The query passes only when the
listener is running, transport is nonclosing and loop progress is within three
seconds. An idle listener is healthy; no synthetic datagram enters the parser or
domain path. Startup before the first loop progression, bind failure, transport
closure/error, stop and stale loop progress do not pass. A blocked event loop
cannot answer the challenge within the client deadline.

The default socket is `dotmac-syslog-<uid>/health.sock` beneath the canonical
system temporary directory selected by Python's `tempfile.gettempdir()` (commonly
`/tmp`). The listener and query must share the temporary-directory environment,
or use an explicit socket path. Set `SYSLOG_HEALTH_SOCKET` in the listener and
query, or pass `--socket` to the query, for another absolute path or
multiple instances. `app.config.Settings.syslog_health_socket` owns the optional
environment override, captured at process bootstrap (including the existing
dotenv loader); changing it requires restarting the listener and probe process.
The health owner consumes that typed setting and supplies the private per-UID
default when it is unset. Empty or relative overrides fail path admission rather
than falling back. `--socket` takes precedence for that query only.
Its immediate parent must be owned by the service UID,
mode 0700 and free of symlink traversal. The socket and lifetime lock are mode
0600. Run probes as that same UID and within the same local mount/network/clock
namespace. Same-UID processes are trusted; this is not an authenticated remote
health API. No health port is exposed.

Each response echoes a fresh random challenge, contains the current random
process-start generation, and has a monotonic timestamp within the request's
start/end interval. Saved responses and previous-process observations cannot
satisfy a new query. All connect/read/write work shares a finite client deadline
(default two seconds, accepted range greater than zero through five seconds).
Requests/replies and concurrent server handlers are bounded. Output never
includes raw payloads, host credentials or exception strings.

An exclusive `flock` lasts for the health endpoint lifetime. Restart may remove
only a stale socket inode previously recorded under that same lock; a live peer
holds the lock and prevents replacement. Unknown existing paths, symlinks,
permissions and socket collisions refuse startup. Cleanup unlinks only the
socket inode this instance created. SIGKILL after bind but before inode metadata
was recorded may leave an unknown collision, which deliberately requires an
operator to inspect and remove the stale path. The `.lock` file and private
directory remain across normal stops; do not delete a live lock file.

Exit 0 means UDP **receiver** readiness, not successful message handling,
parser/domain/database health, or customer service reachability. Handler errors
retain their existing logging and do not fabricate a healthy domain pipeline.

Both CLIs emit a small JSON outcome, exit 1 for nonhealthy/unavailable evidence,
and exit 2 for invalid CLI arguments. The syslog probe imports no parser/ORM
modules; the poller command imports no RouterOS polling runtime.

## Native observation coordinates

The existing source owners expose frozen coordinate values for later thin
adapter preparation. `PollerSnapshot.observation_identity` returns only the
source `poll_count` and verbatim timezone-aware `observed_at`.
`poller_observation_identity(snapshot=...)` returns `None` when the existing
reader/decoder has no snapshot (including malformed or unavailable input).
Device counts, failure details, receive time and freshness classification do
not contribute coordinates. A stale/future snapshot still identifies its
source event; identity does not grant freshness or healthy standing.

`ListenerReadiness.observation_identity` returns only the existing listener
`generation` and `observed_monotonic`. The query's challenge, same-UID endpoint
and bounded request interval continue to qualify the reply. Readiness reason
is independent of these coordinates. The existing zero-generation synthetic
unavailable/malformed results return `None`, even though they carry a local
monotonic failure time. That time cannot substitute for a listener reply.
A new challenge/read is a new measurement; retain the original source value
when referring to the same measurement again.

These are product-native facts, not a shared health DTO, observation key,
serializer, remote authentication or deployment credential assignment. No
coordinate is added to CLI output, health labels or serialized evidence. The
legacy optional Platform Health projection below is unchanged, including its
payload/receive-time dependent key debt. Its future replacement must use the
verified Starter contract and preserve the entire first-collected observation
on retries. Export treatment for absent native identity remains a separate
adapter contract decision; this preparation manufactures no observation or
fallback identity for that case.

## Platform Health observation documents

Both probe CLIs can alternatively serialize one bounded Starter Platform
Health observation document when the caller supplies an explicit source
reference:

```
python -m app.services.poller_health --max-age-seconds 60 --platform-health-source-ref <authenticated-source-ref>
python -m app.syslog.health --timeout-seconds 2 --platform-health-source-ref <authenticated-source-ref>
```

The document uses the Starter `HealthObservationInput` field names and state
vocabulary (`healthy`, `degraded`, `unhealthy`, `unknown`) for component codes
`sub.bandwidth_poller` and `sub.syslog_receiver`. It is a source-edge
serialization only: Sub does not authenticate the source reference, transport
or persist the document, sign deployment-health evidence, or decide a
deployment from it. A future authenticated Integrator transport must bind
`source_ref` to the deployment identity before Platform Health accepts the
observation; an operator-typed local value is not authenticated evidence.

Local exit semantics remain separate from Platform Health freshness. The
poller command still exits nonzero when its bounded local liveness budget
fails, even though a stale snapshot may serialize its last observed device
state for Platform Health to classify as stale from `observed_at`. A missing
or future poller snapshot serializes as `unknown` at receipt time. Poller
documents contain only aggregate device counts, never device identifiers,
names, hosts or failure-row details. Syslog documents contain only the fixed
readiness reason; the challenge, process generation and monotonic reading are
key-derivation identity and are never serialized as labels or evidence fields.

Default CLI output and exit behavior are unchanged when
`--platform-health-source-ref` is omitted. Invalid source references exit 2.
No descriptor, Compose healthcheck, deployment roster or production transport
is activated by this optional output.

## Validation boundary

Focused tests cover malformed/missing/stale/future evidence, Redis failure,
failed-cycle liveness-only semantics, device degradation, healthy idle receiver,
startup/bind failure, stopped/closed/stalled listener, challenge/generation checks,
private endpoint admission, socket collision, crash recovery and consumer parity.
Architecture guards enforce owner declarations and read-only CLI imports.

Tests must run on GitHub-hosted CI, followed by the repository-prescribed Ruff,
formatting, mypy, import, Bandit, architecture, unit and real migrated PostgreSQL
integration lanes at the exact reviewed commit. No local/named-host tests,
dependencies, Docker, deployment or host operations are part of this source slice.
