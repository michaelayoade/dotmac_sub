# ADR 0002: Manifest-verified owner-command transaction boundary

Status: accepted

Date: 2026-07-19

Decision owner: Michael / Dotmac architecture

Affected systems and domains: Dotmac Sub public write commands, application
coordinators, database-session adapters, and transactional event producers

## Context

Dotmac Sub has several transaction idioms: direct service commits, adapter
commits, the legacy `UnitOfWork`, and auto-committing session context managers.
Documentation says a public command owner completes the business transaction,
but no runtime primitive proves that the caller is contracted, that the
session is clean at entry, or that a nested helper did not commit early.

That ambiguity permits partial commits, caller-owned transactions, and
architecture declarations that are disconnected from execution.

## Decision

`app.services.owner_commands.execute_owner_command` is the standard root
transaction executor for new and migrated public write commands.

- A command has typed business input plus `CommandContext`, carrying command,
  correlation, optional causation and idempotency identifiers, actor, scope,
  and reason.
- `OwnerCommandDefinition` links the command to one exact concern in the typed
  SOT manifest.
- Runtime admission requires a contracted writer, reconciler, projection
  writer, observation collector, authoritative-record owner, or application
  coordinator with the matching transaction mode.
- The session must have no active transaction at entry. A violating caller
  transaction is rolled back and rejected with a stable domain error.
- A nested public command, helper commit, helper rollback, or unclosed nested
  transaction cannot produce a successful outcome.
- The executor commits the owned root transaction before returning. It rolls
  back on every failure and does not translate unexpected exceptions.
- The authoritative state change and event-store/outbox record are staged in
  the same transaction.

Adapters continue to create and close sessions. The non-committing
`owner_command_session` lifecycle context is the standard task-side adapter.
Existing auto-committing session contexts and `UnitOfWork` usages are migration
debt; they are not precedent for new or migrated commands.

## Authority boundary

The registered public service owns the business transaction. The database
session adapter owns only opening, defensive cleanup, and closing. Nested
domain helpers may query, add, delete, execute domain SQL, and flush within the
owner's transaction, but cannot complete it. Event delivery remains owned by
`events.dispatcher` after the atomic commit.

## Consequences

Transaction ownership is executable and tied to the canonical manifest. A
caller that queries before invoking a command must finish that read and pass a
clean session, or open a dedicated command session. Cross-owner atomicity
requires an explicitly contracted application coordinator rather than nested
public commands.

The strict entry rule makes hidden adapter work visible during migration. It
may require splitting read preparation from command execution or moving the
authoritative read into the owner.

## First migration slice

`network.device_projection` is the first contracted runtime user. Its
reconcile command owns the projection write and event transaction, takes a
PostgreSQL transaction-scoped advisory lock, converges by natural key, prunes
orphans, and emits `device_projection.reconciled` version 1. The Celery task
owns only session lifecycle, derives stable command/idempotency identity from
the task delivery ID, and retries transient database-operational failures with
bounded backoff.

## Migration and cutover

- Old paths: service-level direct `commit()` and the task's auto-committing
  session context.
- New paths: typed reconcile command through `execute_owner_command` and a
  non-committing adapter session context.
- Verification: focused transaction behavior, projection behavior, manifest,
  adapter, and architecture tests.
- Cutover gate: no direct transaction completion remains in the migrated owner
  or its task, and projection plus outbox commit/rollback tests pass.
- Fallback retirement: the old direct-commit function signature is removed;
  `network.device_projection` is removed from the legacy manifest baseline.

Other domains migrate one coherent owner slice at a time. Existing legacy
boundaries remain indexed debt until their callers and behavior are verified.

## Verification

- Success is committed and the session is transaction-free before return.
- Operation failures, nested commands, and helper commits roll back all state.
- Active caller transactions and uncontracted owners fail closed.
- The first task cannot call the executor or transaction methods directly.
- The first owner cannot call `commit()`, `rollback()`, or `UnitOfWork`.
- Projection idempotency, freshness, pruning, event, and manifest tests pass.

## Rollback or forward-fix

Forward-fix the command, manifest, or adapter together. Reintroducing direct
service or adapter commit is not an accepted rollback because it restores the
ambiguous transaction path. If the projection command must be disabled, stop
its schedule and serve the last committed projection while repairing the
owner; source device records remain authoritative.

## Review and retirement

- Review date: after three materially different command owners have migrated.
- Retirement condition: none. Supersede this ADR if a replacement executor
  enforces at least the same manifest, transaction, nesting, and event
  invariants.
- Supersedes or is superseded by: none.
