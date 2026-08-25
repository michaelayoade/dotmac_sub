# ADR 0013: Inbox conversation and operations authority moves to the composed modules

Status: proposed

Date: 2026-08-23

Decision owner: Michael

Affected systems and domains: `app/models/team_inbox.py`, the ~45
`app/services/team_inbox_*.py` services, `app/services/chat_session*.py`,
`alembic/`, `docs/SOT_RELATIONSHIP_MAP.md`, `app/services/sot_registry/`,
`docs/PLATFORM_ADOPTION_LEDGER.md`, and the composed distributions
`dotmac-inbox 0.1.0a1` and `dotmac-inbox-operations 0.1.0a3`.

## Context

Sub is the named first adopter of both staffed-inbox modules and the only
qualifying source implementation the extraction was taken from. Publication is
supply, not adoption: `dotmac-inbox-operations 0.1.0a3` was published, tagged at
`5b2798b80f6ac903fb132a0b1c205dd1dde3c528` and recorded in Starter PR #378 with
its own record saying Sub "still needs an exact artifact pin and lock, lineage
composition, backfill, zero-drift shadow verification, a sealed writer switch,
and retirement of local parallel writers before authority moves". None of that
has happened. This ADR rules on it.

### What Sub owns today

`public.inbox_conversations` (30 sibling tables in the same file) is written by
a concentrated but unnamed set of paths. Measured at `origin/dev`
`6860f191b`, not estimated:

| model | files referencing | constructor calls | module-owned field mutations |
| --- | --- | --- | --- |
| `InboxConversation` | 51 | 8 | 21, in 7 files |
| `InboxMessage` | 35 | 13 | — |
| `InboxConversationReadState` | 3 | 2 | — |
| `InboxAgentPresence` | 6 | 2 | — |
| `InboxConversationAssignment` | 16 | 2 | — |
| `InboxConversationQueueEntry` | 7 | 2 | — |
| `InboxTeamRoundRobinCursor` | 3 | 2 | — |

The write surface is small; the READ surface is not. Fifty-one files reading
`InboxConversation` is the constraint that shapes this decision, because a
cutover that requires all of them to change at once is a cutover that never
lands.

### The two modules do not own the whole domain

`dotmac-inbox` owns eleven conversation columns, ten message columns and the
read cursor. `dotmac-inbox-operations` owns queues, routing rules, routing
decisions, FIFO admission, presence, assignment and rotation. Everything else
Sub has — subscriber linkage, team linkage, priority, mute, active, metadata,
labels, macros, templates, media, comments, participants, contact links,
provider observations, AI sessions, SLA, reply reminders, campaigns, saved
filters, audit reconstruction — is a Sub consequence the modules deliberately
exclude, and stays in Sub. This is not a lift-and-shift of `team_inbox.py`.

### Vocabulary agrees more than it disagrees

Verified against the installed distributions, not assumed:

- Conversation status is identical in both: `open`, `pending`, `snoozed`,
  `resolved`.
- Message direction is identical in both: `inbound`, `outbound`, `internal`.
- Queue entry status is identical up to case: `queued|promoted|cancelled`.
- Presence is NOT identical. Sub declares four states
  (`online`, `away`, `on_break`, `offline`); the module declares three
  (`AVAILABLE`, `AWAY`, `OFFLINE`). See "knowing narrowings" below.
- The module's channel registry ships EMPTY. `channel_spec()` raises
  `UnknownChannelError` until a product declares its channels, so Sub's ten
  channels become a Sub-owned declaration rather than a module list.

### Three facts Sub does not currently store on a conversation

The module requires `account_scope`, `thread_key` and a `tenant_id` on every
conversation, and a NOT NULL `message_key` and `occurred_at` on every message.
Sub stores none of the first three on `inbox_conversations` and allows the last
two to be null. They are derivable, but the derivation is the risky part of this
cutover and is ruled on explicitly below rather than left to the migration
author.

## Decision

Authority for conversation facts moves to `dotmac-inbox`, and authority for
queue, routing, presence and assignment decisions moves to
`dotmac-inbox-operations`. Sub keeps its consequences, and keeps its existing
tables as READ PROJECTIONS with one named writer.

### 1. Owner map

| Fact | Owner after cutover | Table |
| --- | --- | --- |
| conversation identity, channel, contact, account scope, thread key, status, status reason, subject, tags, first/last message time, resolved/snoozed time | `dotmac-inbox` | `mod_inbox.conversations` |
| message identity, direction, dedup key, subject, body, transport refs, author, occurrence | `dotmac-inbox` | `mod_inbox.messages` |
| per-operator read cursor | `dotmac-inbox` | `mod_inbox.conversation_read_states` |
| queue definition, routing rule, routing decision, FIFO admission and position, presence, assignment, rotation | `dotmac-inbox-operations` | `mod_inbox_ops.*` |
| subscriber link, primary team link, priority, mute, active flag, metadata, continuation link | Sub | `public.inbox_conversations` |
| labels, macros, templates, media, comments, participants, contact links, provider observations, AI, SLA, reply reminders, campaigns, saved filters, audit reconstruction | Sub | their existing tables |
| service teams, skills, shifts, agent eligibility | Sub (Workforce domain) | `service_teams` and friends |
| provider transport, delivery, receipts | Integrator | unchanged |

### 2. The identity rule: the module row keeps Sub's UUID

`mod_inbox.conversations.id` is the SAME uuid as the existing
`public.inbox_conversations.id`, and `mod_inbox.messages.id` the same as
`public.inbox_messages.id`. The backfill copies identity verbatim and never
mints a new one.

This is the decision that makes the cutover affordable. Twenty-five Sub tables
carry a foreign key to `inbox_conversations.id`; re-keying would rewrite every
one of them and every URL, export and saved filter that has ever quoted a
conversation id to an operator. Keeping the uuid means those foreign keys stay
valid, the module's `conversation_reference` is exactly `str(conversation.id)`,
and drift between the two sides is comparable row-for-row by primary key rather
than by a mapping table that could itself be wrong.

### 3. `public.inbox_conversations` becomes a projection with one writer

The table survives, and its columns split into two disjoint sets:

- **Projected columns** — `channel_type`, `status`, `subject`,
  `contact_address`, `external_thread_id`, `first_message_at`,
  `last_message_at`, `snoozed_until`. Mirrors of module facts. Exactly ONE
  writer: the reconciler in `app/services/inbox_projection_reconciler.py`. No
  service, task, route or migration writes them after the switch.
- **Sub-owned columns** — `subscriber_id`, `primary_service_team_id`,
  `priority`, `is_muted`, `is_active`, `metadata`,
  `continued_from_conversation_id`. Sub facts, written by Sub services as they
  are today. The module has no opinion about them and never will.

`public.inbox_messages` splits the same way: `channel_type`, `direction`,
`subject`, `body`, `external_message_id`, `sent_at`, `received_at` are
projected; `notification_id`, `external_thread_id`, `from_address`,
`to_addresses`, `cc_addresses`, `metadata` stay Sub's.

The projection is what lets fifty-one reader files stay untouched. It is a
CACHE with provenance and repair, not a second authority: the reconciler is
idempotent, rebuilds any row from `mod_inbox` alone, and a drift report is an
alert rather than a merge.

### 4. Tables that are retired outright, not projected

These have no Sub-owned columns worth keeping and no long reader tail, so they
are cut over rather than mirrored:

| Sub table | replaced by |
| --- | --- |
| `inbox_conversation_read_states` | `mod_inbox.conversation_read_states` |
| `inbox_agent_presence` | `mod_inbox_ops.inbox_agent_presence` |
| `inbox_conversation_assignments` | `mod_inbox_ops.conversation_assignments` |
| `inbox_conversation_queue_entries` | `mod_inbox_ops.inbox_queue_entries` |
| `inbox_team_round_robin_cursors` | `mod_inbox_ops.inbox_round_robin_cursors` |

`inbox_routing_events` is NOT in that list. The module's
`inbox_routing_decisions` records the rule that admitted a conversation;
Sub's `inbox_routing_events` records a broader operational event stream with an
evidence grade the module has no column for. Sub keeps it, and the module's
decision row becomes one more thing it can cite.

`inbox_agent_presence_details` is the complementary Sub-owned row, not a
parallel presence owner. It stores only the current roster `away_reason`
(`away` or `break`) and its observation time. The module owns dispatch
availability and capacity; Sub owns the product roster explanation.

### 5. Derivation rules for the three missing facts

These are the cutover's real risk and are ruled here so the migration does not
invent them.

**`tenant_id`** — `app.services.operator_tenant.OPERATOR_TENANT_ID`. Sub is a
dedicated single-operator deployment under ADR-0009 and already sets this exact
value as the `app.current_tenant` GUC on every root transaction.

**`account_scope`** — resolved by this ladder, in order, first hit wins:

1. `inbox_provider_observations.provider_account_scope`, for the observation
   joined to this conversation. This is the strongest evidence: it is what the
   provider actually told us.
2. `team_inbox_channel_routes.account_scope`, matched on
   `(channel_type, is_active)` where the conversation's primary team matches the
   route's team and exactly one active route qualifies.
3. For channels Sub declares with `Transport.INTERNAL` — today `note` and
   `field_job` — the literal `sub:internal`. These never arrived at a connected
   account, and inventing one would be worse than declaring the absence. The
   set is READ FROM THE DECLARATION (`INTERNAL_TRANSPORT_CHANNELS`), never
   written out a second time here. `chat_widget` is deliberately NOT in it: a
   widget session does arrive somewhere, so it resolves on rung 1 or 2 or it is
   refused.
4. **No fourth rung.** A conversation that reaches here is REFUSED by the
   backfill and reported by row id. It is not defaulted, not guessed, and not
   silently assigned to the first route.

**`thread_key`** — `dotmac_inbox.threading.thread_key()`, called with the
resolved `account_scope`, never reimplemented in SQL. The module's function is
the definition; a second implementation in a migration is exactly the drift this
extraction existed to end.

`thread_key` is UNIQUE per tenant. Sub's data has never been constrained that
way, so collisions are possible and their number is unknown until measured
against real data. **The backfill refuses on the first collision** and reports
the colliding set. Merging two conversations because their derived keys agree is
a data decision, not a migration decision, and it is Michael's to make against a
census — not a `ON CONFLICT DO NOTHING` clause.

**`message_key`** — `dotmac_inbox.threading.dedup_key().value`, same rule, same
reason. Sub's `external_message_id` is null on every outbound and internal
message, so the module's declared fallback to a content fingerprint is what
covers them.

**`occurred_at`** — `coalesce(received_at, sent_at, created_at)`. All three are
present on every Sub message; `created_at` is NOT NULL with a default, so the
coalesce is total and no message is refused for want of a timestamp.

### 6. Knowing narrowings

Two facts get smaller in the move, and both are recorded rather than discovered
later:

- **Presence `on_break` maps to `AWAY`.** The module has three states and Sub
  has four. `on_break` and `away` both mean "present but not assignable", which
  is the only distinction the module's dispatch reads, so the assignment
  behaviour is identical. The `on_break`/`away` difference is a roster,
  adherence and staffing-report fact. Sub keeps it in
  `public.inbox_agent_presence_details.away_reason`: `on_break` projects to
  `AWAY + break`, while ordinary away projects to `AWAY + away`. The exhaustive
  two-input round-trip test proves the operator-facing state survives.
### 6a. Four module gaps that block activation, found during P5

Rewiring the writers surfaced four facts Sub expresses and `dotmac-inbox`
currently cannot. All are small module additions. None is worked around here,
because every available workaround corrupts something quietly.

The first two were found routing field ASSIGNMENTS; the second two only appeared
once conversation and message ADMISSION was routed as well — which is the
concrete reason the census was wrong to exclude constructors.

**Indefinite snooze.** Sub supports "snooze until reply": SNOOZED with no wake
time. `dotmac_inbox.lifecycle` requires `snoozed_until` for that status and
raises without it. The rejected workaround was a far-future sentinel timestamp,
which would read as a real deadline to every report, every sort and every
operator looking at the conversation. `team_inbox_operations.snooze_until_reply`
now routes through the seam and therefore FAILS LOUDLY at MODULE stage — which
is the correct behaviour for an unsupported transition, and is why this path no
longer appears in the writer baseline. The module needs to accept an indefinite
snooze carrying a reason code.

**Delivery outcome learned late.** Sub stamps `sent_at` and
`external_message_id` AFTER the provider accepts a message. `record_message`
takes the transport ref at admission because it feeds the dedup key, so
learning it later would mean recomputing `message_key` — mutating a unique
column that other rows have already been deduplicated against. The module needs
a delivery-outcome operation that updates the transport ref and occurrence
WITHOUT touching `message_key`; the two are different facts and the module
currently conflates them at one moment in time.

**No typed internal-principal identity.** The module threads on
`(channel, account_scope, contact)`, and `contact` is an external party's
address or opaque provider id. Sub has conversations with no external party at
all: an internal note, a field-job chat with a subscriber in the portal, a
widget session before the visitor identifies. `inbox_writes` now REFUSES these
at MODULE stage (`MissingConversationContact`) rather than admitting them under
an empty or synthesised contact — an empty contact puts every anonymous visitor
in one thread, and a synthesised one makes each unmergeable when the visitor
later identifies. The module needs a typed internal principal.

**An internal channel cannot carry provider thread identity.** `ChannelSpec`
refuses `Transport.INTERNAL` together with `ThreadIdentity.PROVIDER`, on the
assumption that internal channels have no thread objects. Sub's `field_job`
disproves it: each work order is its own thread, with its own id. Declared
`DERIVED` — the only option the validation leaves — every work order for one
subscriber collapses into a single conversation. So `field_job` cannot cut over
until the module admits a thread id on an internal transport.

Until all four land, `app/services/communication_intents.py`,
`app/services/team_inbox_outbound.py`, `app/tasks/notifications.py`,
`app/services/team_inbox_field_job.py` and `app/services/team_inbox_operations.py`
keep writing inbox rows directly, and they stay on the writer baseline naming
exactly which gap holds each one.

- **`inbox_conversations.priority` has no module counterpart.** The module
  orders a queue by FIFO position, deliberately — a durable position is the
  answer to "where am I in the line" and a mutable priority is not. Sub keeps
  `priority` as its own column and it stops influencing queue order at the
  switch. This is a BEHAVIOUR CHANGE, not a refactor, and it needs its own
  acceptance from whoever owns contact-centre policy before phase P5.

### 7. Queues bind to service teams; neither owns the other

Workforce owns `service_teams`; `dotmac-inbox-operations` owns
`mod_inbox_ops.inbox_queues`; Sub owns the binding between them, in a new
`public.inbox_queue_bindings (service_team_id, queue_id)` with a unique
constraint on each side.

Reusing `service_team.id` as the queue id was rejected. It is one fewer table
and it reads as free, but it welds a Workforce identifier into a shared module's
primary key, so a team that later needs two queues — or a queue with no team,
which is what an overflow or after-hours queue is — cannot exist without a
migration of the module's own rows.

`agent_reference` is `str(person_id)` and `conversation_reference` is
`str(conversation_id)`. Both are declared once, in the adapter, and never
formatted at a call site.

### 8. Sub declares its channels

`dotmac_inbox.channels` ships an empty registry on purpose. Sub declares its ten
channels once, at import, in `app/services/inbox_channels.py`, with each
channel's four traits stated. Nothing under `app/` calls `channel_spec()`
without that module having been imported, and the traits are Sub's to choose:
they encode, for the first time in one place, the per-channel threading and
deduplication rules that are currently spread across the receive services.

## Invariants

- `mod_inbox.conversations.id` equals `public.inbox_conversations.id` for every
  row, forever. A conversation that exists on one side and not the other is a
  drift finding, never a normal state after the switch.
- No Sub service, task, route or migration writes a projected column after P5.
  The reconciler is the only writer, and an architecture test enumerates it.
- No Sub code writes a `mod_inbox*` table directly AT RUNTIME. Every runtime
  write goes through the module's own service functions.
  **One temporary declared exception:** the backfill in
  `app/services/inbox_backfill.py`. `dotmac_inbox.service.create_conversation`
  mints its own uuid and offers no way to supply one, because it is a runtime
  entry point and minting identity is its job — so establishing history under
  the identity rule of §2 is not something the module's API can express. The
  backfill therefore writes through the module's own MAPPED CLASSES (never raw
  SQL, so a column rename upstream is an import error rather than a silent
  no-op). The exception covers every historical module row it imports, not only
  conversations; the architecture test enumerates the mapped-class vocabulary
  and pins construction to this one file. An existing id is a replay only when
  every imported fact agrees; otherwise the whole transaction refuses as
  drift.

  Starter now declares the permanent typed owner seams in `dotmac-inbox`
  0.1.0a2 and `dotmac-inbox-operations` 0.1.0a4. Those source versions are not
  installable evidence. This bridge retires as soon as the exact releases are
  registry-verified and pinned in Sub; a version-sensitive architecture gate
  fails if the mapped-class writes survive either pin.
- `thread_key` and `message_key` are computed by `dotmac_inbox.threading` and
  nowhere else. No SQL expression, no second Python implementation.
- The backfill refuses rather than defaults. An underivable `account_scope` or a
  duplicate `thread_key` fails the run with the offending ids, and never
  produces a row.
- The reconciler is idempotent and total: running it twice changes nothing, and
  running it against an emptied projection rebuilds every projected column from
  `mod_inbox` alone.
- Sub's own migration chain stays single-headed; the two module lineages are
  separate heads, per ADR-0011.
- `public` stays Sub's and `mod_inbox`/`mod_inbox_ops` stay their modules'. No
  cross-plane foreign key exists in either direction except the module's own
  `tenant_id` reference to `public.tenants`, which ADR-0009 already hosts.

## Consequences

**Data.** Roughly 24,000 historical conversations and their messages get a
derived `account_scope`, `thread_key` and `message_key` they have never had.
The derivation is auditable and the refusal path is loud, but the census of how
many rows reach rung 4 or collide is UNKNOWN until it is run against real data,
and this ADR does not pretend otherwise. That census is the gate on P3.

**Operational.** Two new schemas, two new migration heads, and a reconciler that
must run on a schedule. A reconciler that stops running degrades a cache, not an
authority — reads go stale, writes stay correct — which is the whole reason for
choosing a projection over a dual write.

**Behaviour.** Queue order stops honouring `priority` (§6). Presence dispatch
keeps its three-state contract while the Sub roster retains the break/away
distinction (§6). Everything else is intended to be behaviour-identical, and
the shadow phase exists to prove it rather than to assert it.

**Compatibility.** Fifty-one reader files are untouched. The API surface, the
realtime payloads and the operator UI see the same columns with the same
meanings. This is deliberate and it is the reason a cutover of this size is
attemptable at all.

**Security.** The module tables are tenant-scoped with FORCEd RLS, which is
stricter than the `public` inbox tables have today — Sub's inbox tables have no
`tenant_id` at all and rely on the deployment being single-operator. The move
therefore raises the isolation floor rather than lowering it.

**Team.** `app/services/inbox_module/` becomes the only place Sub speaks to the
modules, and a diff there is an authority statement. The forty-five
`team_inbox_*` services keep their names and their jobs; what changes is which
of them may write.

### Rejected alternatives

**Dual-write Sub and the modules indefinitely.** Tempting because it needs no
reader changes and no cutover moment. Rejected because it is two authorities by
construction: every write path must keep them consistent, every failure leaves
them inconsistent, and there is no answer to "which one is right" — which is the
exact defect the source-of-truth standard exists to prevent. The shadow phase
uses a bounded, dated dual write and P5 ends it; that is different from adopting
one as the design.

**Move the whole of `team_inbox.py` into the modules.** Rejected: the modules
deliberately exclude AI, SLA, media, participants and campaigns, and pushing
them in would make a shared module carry one product's contact-centre policy.
The extraction rule forbids harvesting what merely looks alike.

**Retire `public.inbox_conversations` entirely and repoint all readers.**
Correct in the long run and rejected as a prerequisite: fifty-one files and
twenty-five foreign keys is not a cutover, it is a rewrite, and it would block
the authority move behind unrelated work. The projection is explicitly a
transitional shape whose retirement is a later, separate decision.

## Migration and cutover

- **Old owner and paths:** `app/services/team_inbox_*.py` writing
  `public.inbox_*` directly; no named owner for routing, admission or rotation.
- **New owner and paths:** `dotmac_inbox.service` and
  `dotmac_inbox_operations.service`, reached ONLY through
  `app/services/inbox_module/`. `app/services/inbox_projection_reconciler.py`
  owns the projected columns.
- **Backfill/repair:** a repeatable, restartable command (not a one-shot
  migration body) that derives per §5, refuses per §5, and can be re-run to
  convergence. Exact existing rows replay; same-id/different-fact rows refuse
  and roll back. Repair is the same code path as backfill.
- **Shadow or verification phase:** dual write behind a sealed switch, plus a
  comparator that reports per-field drift between `public.inbox_*` and
  `mod_inbox*`. The gate is ZERO drift over a full business cycle including a
  weekend, not a single green run.
- **Cutover gate and evidence:** (a) backfill converged with zero refusals and
  zero collisions; (b) comparator reports zero drift for the agreed window;
  (c) the `priority` behaviour change in §6 is accepted by name; (d) Sub's own
  chain still single-headed and both module heads applied; (e) **the projected-
  column writer baseline is EMPTY**, which means both §6a module gaps are
  closed and released. (e) is not bureaucracy: at MODULE stage the reconciler
  rebuilds the projection from `mod_inbox`, so a remaining Sub writer's value
  is silently discarded on the next reconcile — a delivery receipt would just
  vanish. The baseline counts direct `InboxConversation(...)`/`InboxMessage(...)`
  CONSTRUCTION as well as field assignment: an earlier version excluded
  constructors on the grounds that P6 retires them, which is circular, because
  P6 runs after the activation this gate authorises. With constructors invisible
  the baseline could reach zero while every admission path still created
  Sub-only rows. `app/services/inbox_authority.activate` enforces (a)–(c) in
  code; (d) and (e) are enforced by the migration and architecture suites.
- **Fallback retirement:** the local writers are removed in P6, after the
  comparator has been clean with the module as sole writer. The Sub tables are
  narrowed to their Sub-owned columns in the same change, so a re-introduced
  local writer fails to compile rather than silently forking authority.
- **Schema contract step:** additive only until P6. P3 adds
  `inbox_queue_bindings` and the product-owned
  `inbox_agent_presence_details`; P6 drops the five retired tables and the
  projected columns that stop being written, each in its own expand/contract
  step.

## Verification

- **Architecture:** every composed lineage is pinned and bound
  (`tests/architecture/test_composed_module_lineages.py`, already generic over
  `_COMPOSED_MODULE_LINEAGES`); no `app/` module writes a projected column
  outside the reconciler; no `app/` module imports a module's models to write
  them; `thread_key`/`dedup_key` have exactly one call site each.
- **Behaviour:** the adapter's channel declarations round-trip every one of
  Sub's ten channels through `thread_key` and `dedup_key`; dispatch presence
  plus Sub's roster reason round-trip all four operator states; a resolved
  conversation reopened by an inbound message follows the module's transition
  table.
- **Reconciliation:** the reconciler is idempotent (run twice, no change) and
  total (empty the projection, rebuild it, compare byte-for-byte).
- **Migration:** `alembic upgrade heads` from the deployed revision against a
  production-shaped database leaves Sub single-headed with `ib` and `io` applied;
  the backfill refuses a deliberately underivable row and a deliberately
  colliding pair.
- **Operational:** the comparator emits a drift metric; a stopped reconciler
  raises staleness, not corruption.
- **Isolation:** cross-tenant RLS canaries for both modules' tables against real
  PostgreSQL, per the existing testing model.

## Rollback or forward-fix

P0–P4 are reversible with no data consequence: the module tables are additive,
the backfill is re-runnable, and until P5 Sub's tables are still the authority.

P5 is the point of no easy return. Rolling back after it means the module rows
written since the switch have no Sub-side original, so the recovery is the
reconciler run in REVERSE — rebuilding `public.inbox_*` from `mod_inbox*` — which
is exactly the code path P4 proves. That is why the reverse direction is built
and tested in P4 rather than being written under pressure.

P6 is not reversible without restoring from backup, which is why it is a
separate change gated on a clean comparator with the module as sole writer.

## Review and retirement

- Review date: at the P5 cutover gate, or 2026-11-23, whichever is first.
- Retirement condition: superseded when `public.inbox_conversations` and
  `public.inbox_messages` are retired entirely and their readers repointed at
  the module, which this ADR explicitly does not attempt.
- Supersedes or is superseded by: extends ADR-0011 (module lineage composition)
  and ADR-0009 (operator-tenant bridge). Supersedes nothing.
