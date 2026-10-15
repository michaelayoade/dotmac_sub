# Deployment writer drain evidence

Status: source component; hosted acceptance and integrated release approval pending.

`app.services.deployment_writer_drain` owns the read-only count observation and
bounded stable-empty policy. `deployment_writer_drain_transport` admits pinned
Celery/Redis responses into that contract. `scripts.observe_writer_drain` is
only the CLI/configuration/reporting adapter. Neither component owns financial
authority, container identity, producer shutdown, worker shutdown, rollback,
maintenance, schema changes, or permission to deploy. The integration
coordinator owns the executable SOT declaration and relationship-map update.

## Inputs and safe output

`DrainRequest` is frozen: explicit `DrainMode`, exact unique `WorkerNodeId`
inventory, the complete shared `DEPLOYMENT_QUEUE_NAMES` inventory represented
as `DeploymentQueueName` values, and validated `DrainBudgets`. Full mode requires
1..64 unique incumbent worker node IDs. Broker-only mode forbids worker IDs.
Every full observation requires each expected node exactly once for **each**
active, reserved, and scheduled inspection. Queries broadcast to every
broker-visible node, so destination filtering cannot hide an extra worker.
Missing, duplicate, unexpected,
flattened, error-shaped, or malformed replies refuse immediately.

Observations contain UTC start/completion timestamps, per-node counts for all
three populations, per-queue ready counts, and Redis unacknowledged hash/index
counts. Queue counts include every configured Redis priority subqueue. Counts
are nonnegative integers; booleans, invalid types, and incomplete observations
refuse. No task ID, name, argument, customer data, task payload, broker URL,
credential, or transport error text enters the outcome or JSON report. Raw
library replies exist transiently only while validating/counting them.

The broker URL is private adapter configuration (`repr=False`), never an input
to the domain policy, a CLI flag, or serialized evidence. Resolution follows
the explicit environment-owned precedence in
`scheduler_config.get_celery_config`: `CELERY_BROKER_URL`, then `REDIS_URL`.
Absent explicit configuration refuses rather than inferring localhost/default
database. No import of `app.celery_app`, `scheduler_config`, `app.config`, or
`app.db` is required. The scheduler resolver opens a database session, so it
cannot be reused directly by this probe. Its source contract must remain
aligned with this narrow resolution. Unknown configuration overlays refuse.

The initially proven transport is plain `redis://hostname/explicit-db`, without
URI query/fragment, Sentinel, TLS, or global key prefix. Nonempty prefixes
refuse before connecting or inferring any empty queue. The typed adapter
configuration can declare actual unacknowledged keys and priority steps;
there is no generic transport-options mapping. The deployment CLI uses the
incumbent default keys/steps because the scheduler source declares no options.
Any future transport/configuration change requires new proof before use.

## Pinned API evidence

Source inspected without importing or executing the installed packages:
Celery **5.4.0**, Kombu **5.6.2**, Redis client **5.0.4**, confirmed by installed
distribution metadata. The adapter verifies these exact versions at runtime.

- Celery `app/control.py:Control.broadcast` publicly accepts `connection`,
  `reply`, `timeout`, `destination`, `limit` and returns raw replies. Worker
  `control.py` declares commands `active`, `reserved`, `scheduled`; only active
  implements `safe=True`. These are the commands used, rather than guessing
  historical `dump_*` aliases.
- Kombu `pidbox.py:Mailbox._broadcast` defaults the reply limit to the number
  of destinations, which can hide a duplicate after the apparent complete
  reply set. Explicit `limit=0` collects raw replies until inactivity timeout.
  No destination filter is applied, and `Inspect._prepare` flattening is never
  used. `_collect` timeout is per event,
  and `_publish` retries; neither independently bounds a whole request.
- Kombu `virtual/base.py:944-946,989-1002` and `redis.py:588-612,745,1349-1353`
  show that public `polling_interval` controls Redis BRPOP and unsuccessful-poll
  sleep, while retries reuse the original event timeout. Default one-second
  BRPOP can overrun a two-second request guard. Only the control connection uses
  `min(0.05, inspection_timeout * 0.05)` polling; financial transport settings
  remain unchanged. `redis.py:1103-1109` also waits for pending BRPOP on close.
  The real Redis sensitivity canary reinstates the default polling and requires
  timeout refusal, without increasing the declared request budget.
- Kombu `transport/virtual/base.py:Channel.queue_declare(passive=True)` first
  checks existence and returns `_size`. Redis `transport/redis.py:_size`
  pipelines `LLEN` for all priority steps; `_has_queue` checks those steps.
  Redis deletes empty lists, so exact known-queue passive absence is empty.
  Only exact `ChannelError` with code string `404`, method signature `(50,10)`,
  method name `Channel.queue_declare`, and the expected queue/vhost absence
  text qualifies. Arbitrary 404, auth/network/protocol/wrong-type errors refuse.
- The Kombu 5.6.2 global-prefix wrapper does not prefix `EXISTS`; consequently
  prefixes are explicitly unsupported before absence inference.
- The actual transport channel's configured `unacked_key` and
  `unacked_index_key` are read using public Redis `HLEN` and `ZCARD`. Each must
  independently equal zero, including orphan hash-only or index-only states.
- Passive Redis `_new_queue` does not create a queue. The broker channel never
  consumes tasks, so shutdown has no deliveries to restore. There is no task
  purge, revoke, acknowledge, restore, visibility mutation, or worker boot.
  Full inspection uses a **separate** connection with acknowledgement emulation
  disabled only for that probe. Otherwise Redis pidbox polling could call
  `qos.restore_visible` against global financial unacknowledged keys. The
  financial worker configuration and broker-count channel are unchanged.
  Full inspection necessarily publishes transient Celery control traffic and
  consumes its replies; it never publishes or consumes financial task work.

## Deadline and stability semantics

Inspector request timeout is positive and at most 5 seconds. Drain budget
defaults to 600 seconds, is at most 3600 seconds, and must exceed the stability
interval. Stability defaults to 5 seconds and must be between 5 and 60 seconds.

The policy uses a monotonic overall deadline. Each complete observation is
bounded by the remaining budget, covering imports/configuration, all worker
queries, every queue, both unacknowledged counts, retries and cleanup. The
POSIX main-thread adapter uses `ITIMER_REAL` with a private `BaseException`
alarm that bypasses library retry handlers; repeating expiry also interrupts
blocking cleanup. Each inspector request has its own nested hard guard. The
pidbox inactivity timeout is 75% of that request budget to leave guard
headroom; short control-only Redis polling keeps empty polls and pending-BRPOP
cleanup within that headroom. The five-second maximum applies to each raw
inspection request; the complete observation uses the remaining overall drain
budget. Unsupported runtime, a pre-existing alarm, and any timeout refuse.
Expiry cleanup has at most the 50ms repeating-alarm scheduling interval plus
normal interpreter scheduling; it never intentionally waits without a bound.

`wait_for_stable_writer_drain` requires **two complete zero observations**.
The stability interval begins after the first observation finishes. A busy
observation resets the candidate pair. Unknown/malformed/unavailable evidence
immediately refuses; it is never retried into a fabricated success. The
policy will not accept a second zero completed at or after its deadline.
Only the latest complete observation and the first zero of a successful pair
are retained; evidence does not grow with polling duration.

These are bounded sequential observations, not an atomic broker snapshot or a
guarantee against future enqueues. The deployment owner must stop producers
before the full proof, keep incumbent workers consuming during it, gracefully
stop every captured worker, prove those exact containers stopped, and then
obtain broker-only stable-empty evidence. Broker-only evidence has no worker
coverage and must never substitute for the earlier full proof.

Worker names are identities asserted by trusted Celery pidbox replies. Redis
remote control has no cryptographic worker-node signing: a party with broker
write access could forge an **expected** node name and empty reply. Unexpected
names and duplicate replies refuse, but counts cannot authenticate such an
exact-name forgery. The separate container/broker trust and identity proof is
required; this limitation was escalated to the architecture/integration owners.

## CLI contract

```text
python -m scripts.observe_writer_drain inspect|wait-for-stable-empty
  --mode workers-and-broker|broker-only
  [--expected-node CELERY_NODE ...]
  [--inspection-timeout 5] [--drain-budget 600] [--stability-interval 5]
```

`--mode` is mandatory. Repeat `--expected-node` for every captured incumbent
Celery node in full mode; it is forbidden in broker-only mode. JSON is one
bounded line: `schema_version=1`, explicit `mode`/`outcome`, closed `refusal` or
null, safe `request` binding for valid input, `observation` or null, and
`previous_zero` or null. Invalid unparsed input reports mode null and refused.
Exit 0 means observed-empty for inspect, or stable-empty for wait. Exit 1 means
observed-busy or deadline-exceeded. Exit 2 means refused. Deployment must check
the schema, exact mode and **stable-empty** outcome as well as the exit code.

## Required hosted acceptance

Unit canaries cover all populations/queues, late enqueue resetting stability,
deadline rejection, exact coverage, malformed/forged unexpected nodes,
unsupported prefix/config, exact empty404, and safe bounded JSON.
`tests/integration/writer_drain_acceptance.py` requires migrated
PostgreSQL under the repository integration policy plus an exclusive hosted
Redis service: `TEST_WRITER_DRAIN_REDIS_DISPOSABLE=1` and
`TEST_WRITER_DRAIN_REDIS_URL=redis://redis/10` (or an explicitly allowed local
hosted service and DB10..15). Missing service is a failure, never a skip or a
mock substitution. The fixture owns and cleans only that exclusive test DB.
It exercises real priorities/all11 queues, orphan unacknowledged keys, custom
keys, wrong-type refusal, absence, read-command restrictions, a real Celery
worker with active/reserved/ETA tasks, missing/extra nodes, expired financial
unacknowledged entries remaining untouched by full inspection, nested control
event-flood/blocked-cleanup deadlines, and a fresh-process
no-scheduler/database-import canary.

The dedicated `Writer Drain Acceptance (Redis and Celery)` job in CI owns this
explicit file target (outside ordinary `test_*.py` shard discovery), an
exclusive Redis DB10, and the existing migration/bootstrap owners. The required
`Tests` aggregate requires its success for every non-documentation change;
there is no optional service skip or advisory acceptance lane.

No local runtime/test/DB/Docker/SSH execution was performed. Source is not
deployable until hosted Redis/Celery acceptance, SOT integration, independent
review, shell/rollback components, migration691, complete rehearsal and
Michael's exact release approval are complete.


Hosted Celery testing uses distinct controller hostnames for the extra-worker
canary. Celery 5.4 `contrib/testing/worker.py:155-168` always supplies an
anonymous PID/host name, so two workers in the same process otherwise collide.
The explicit controllers replace that value while exercising real broadcasts.

Hosted acceptance fixture binds the disposable base Celery broker URL and clears
split read/write overrides, then asserts every worker effective broker URL matches
that exclusive Redis database. The CLI retains its refusal of split overlays.
Signal canaries run in isolated child processes because the hosted pytest timeout
owner holds a process alarm; the production probe still refuses an existing alarm.
