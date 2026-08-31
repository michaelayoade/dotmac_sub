# ADR 0013: Conversation authority moves to the composed `dotmac-inbox`

Status: proposed

Date: 2026-08-30

Decision owner: Michael

Affected systems and domains: `app/models/team_inbox.py`, the
`app/services/team_inbox_*.py` services, `app/services/inbox_channels.py`,
`app/services/sot_registry/domains/notifications_communications.py`,
`alembic.ini`, `app/commercial_module_prereqs.py`,
`docs/PLATFORM_ADOPTION_LEDGER.md`, `docs/SOT_RELATIONSHIP_MAP.md`, and the
composed distribution `dotmac-inbox 0.1.0a1`.

The full design — field-by-field reconciliation, channel traits, backfill
ladder, projection split, realtime boundary — is
[`docs/designs/INBOX_MODULE_ADOPTION.md`](../designs/INBOX_MODULE_ADOPTION.md).
This ADR rules; the design explains.

## Context

`dotmac-inbox` is a product-first extraction whose single qualifying source is
Sub's own Team Inbox (starter `docs/inventories/inbox-sources.md`, starter
ADR-0052, which names Sub cutover 1). The package is published as `0.1.0a1` and
has **zero consumers**. Publication is supply, not adoption: an extraction
nobody consumes has never been tested against the product it was taken from.

Sub owns the conversation aggregate today in `public.inbox_conversations`,
`public.inbox_messages` and `public.inbox_conversation_read_states`, with named
owners in the executable SOT registry
(`communications.team_inbox_threads`, `communications.team_inbox_status`,
`communications.team_inbox_operator_state`). Those owners are correct and
working. Three things about the current shape are not:

1. **No tenancy.** None of the three tables has a `tenant_id` and none has RLS.
   Isolation rests entirely on the deployment being single-operator.
2. **Message identity is globally scoped for every channel.**
   `uq_inbox_messages_inbound_external` is a partial unique index on
   `(channel_type, external_message_id)`. That is right for an RFC 5322
   `Message-ID` and wrong for a `wamid`, a Messenger id or a comment id, each of
   which is meaningful only inside the account it was delivered to. The second
   arrival at a second connected account is dropped today.
3. **Channel behaviour is decided by name, in about fifteen places.** Two
   "opaque endpoint" sets in different services do not even agree with each
   other (`team_inbox_channel_receive._OPAQUE_CONTACT_CHANNELS` versus
   `team_inbox_participants._OPAQUE_ENDPOINT_CHANNELS`).

A prior attempt at this cutover exists on the archived branch
`archive/worktrees/2026-08-25/sub-inbox-cutover`. It was preserved, not merged.
Its findings — the identity rule, the derivation ladder, the projection split
and four module gaps — are carried forward here rather than rediscovered.

## Decision

Authority for conversation facts moves to `dotmac-inbox`. Sub keeps every
consequence.

This ADR authorises the composition and the channel declarations
**immediately**, and authorises nothing else. Each later phase needs its own
approval against the gates below.

### 1. Owner map

| Fact | Owner after cutover | Table |
| --- | --- | --- |
| conversation identity, channel, contact, account scope, thread key, status, status reason, subject, tags, first/last message time, resolved/snoozed time | `dotmac-inbox` | `mod_inbox.conversations` |
| message identity, direction, message key, subject, body, transport refs, author, occurrence | `dotmac-inbox` | `mod_inbox.messages` |
| per-operator read cursor | `dotmac-inbox` | `mod_inbox.conversation_read_states` |
| subscriber link, primary team link, priority, mute, active flag, metadata, continuation link, message addressing and notification link | Sub | `public.inbox_*` |
| leads, tickets, field jobs, AI intake, participants, contact links, labels, macros, templates, media, comments, saved filters, reply reminders, campaigns, provider observations, delivery receipts | Sub | their existing tables |
| queues, routing, presence, assignment, rotation | **undecided — out of scope**; `dotmac-inbox-operations` is a separate decision | — |
| provider transport, delivery, receipts | Integrator | unchanged |

### 2. Composition is not adoption, and this change claims only composition

Sub pins `dotmac-inbox==0.1.0a1`, composes the `ib` lineage so `mod_inbox`
exists, and declares its channel traits. No `app/` module imports the module's
service, models or history seam; nothing writes a `mod_inbox` row; no runtime
path changes. `tests/architecture/test_inbox_module_composition.py` makes that
sentence checkable rather than promised, and it fails on the first line of the
writer slice — which is correct, because that slice amends this ADR in the same
commit.

### 3. The module row keeps Sub's UUID

`mod_inbox.conversations.id` is the same UUID as
`public.inbox_conversations.id`, and likewise for messages and read states. The
backfill copies identity verbatim and never mints one. Sub foreign keys, URLs,
exports and saved views that already name a conversation stay valid, and drift
is comparable row-for-row by primary key rather than through a mapping table
that could itself be wrong.

The identity-preserving seam is `dotmac_inbox.history`, not
`create_conversation`. Runtime creation correctly mints identity; adoption must
not.

### 4. Sub's tables become projections with exactly one writer

`public.inbox_conversations` and `public.inbox_messages` survive. Their columns
split into a projected set (mirrors of module facts, written only by the
reconciler) and a Sub-owned set. That is what lets the large read surface stay
untouched. `public.inbox_conversation_read_states` is retired outright rather
than projected: one writer, one reader, no Sub-owned columns.

A projection is a cache with provenance and repair, never a second authority.

### 5. Sub declares its channels; the module never learns their names

`app/services/inbox_channels.py` declares nine channels with their four traits.
`field_job` is deliberately NOT declared, because the truthful declaration — an
internal transport whose thread identity is the work order — is one the module
refuses to construct, and the only constructible alternative would merge every
work order for one subscriber into a single conversation. An undeclared channel
fails loudly at the first call; a wrongly declared one fails silently at
cutover.

### 6. Four module gaps block activation and are not worked around

`MODULE-GAP-1` no typed internal principal (`contact` is NOT NULL);
`MODULE-GAP-2` no indefinite snooze; `MODULE-GAP-3` no delivery-outcome
operation that spares the message key; `MODULE-GAP-4` no supplied thread
identity on an internal transport. Each rejected workaround corrupts something
quietly; the design records which. All four must be released before the writer
switch.

### 7. `dotmac-inbox-operations` is a separate decision

This ADR rules on conversations, messages and read cursors only. Assignment,
queueing, routing and presence stay with Sub's existing owners and are not
implicitly authorised to move.

## Invariants

- `mod_inbox.conversations.id` equals `public.inbox_conversations.id` for every
  row, forever. A row on one side and not the other is a drift finding.
- `thread_key` and `message_key` are computed by `dotmac_inbox.threading` and
  nowhere else. No SQL expression, no second Python implementation.
- The backfill refuses rather than defaults. An underivable `account_scope` or
  a duplicate `thread_key` fails the run with the offending ids.
- Exactly one `app/` module names `dotmac_inbox`, and until the writer slice it
  may name only `dotmac_inbox.channels`.
- No Sub service, task, route or migration writes a projected column after the
  switch; the reconciler is the only writer and a ratchet enumerates it.
- The reconciler is idempotent and total: run it twice and nothing changes;
  empty the projection and it rebuilds from `mod_inbox` alone.
- `dotmac-inbox` never grows a websocket, a connection manager or a publish
  call. Realtime payloads are built from the owner's returned rows and
  published after commit by Sub.
- `public` stays Sub's and `mod_inbox` stays the module's. No cross-plane
  foreign key exists except the module's own `tenant_id` reference to
  `public.tenants`, which ADR-0009 already hosts.
- Sub's own migration chain stays single-headed; `ib` is a separate head, per
  ADR-0011.

## Consequences

**Data.** Every historical conversation acquires a derived `account_scope`,
`thread_key` and `message_key` it has never had. The derivation is auditable
and the refusal path is loud, but the census of how many rows refuse or collide
is UNKNOWN until it runs against real data. That census gates the backfill, and
its result may change the derivation rules rather than merely satisfy them.

**Operational.** One new schema and one new migration head now; a scheduled
reconciler later. `scripts/deploy.sh` verifies the module schema contract with
the restricted migration connection before Alembic, so `mod_inbox` must exist
before the next deploy can proceed.

> **Corrected 2026-08-31.** This paragraph previously said the next deploy
> needed an elevated `BOOTSTRAP_DATABASE_URL` bootstrap supplied once. That
> instruction is now refused: a persisted elevated DSN would arm auto-repair on
> every deployment. The deploy owns the repair, using the least-privilege
> `dotmac_schema_bootstrap` credential, and reports `already_satisfied`,
> `repaired` or `blocked`. See the 2026-08-31 amendment to ADR-0011.

**Behaviour.** Nothing changes in this slice. At the writer switch, account-
scoped message identity begins admitting provider ids that the global index
drops today. That is a correction, and the shadow comparison must predict and
count it rather than discover it.

**Security.** The module tables are tenant-scoped with forced RLS, which is
stricter than Sub's `public.inbox_*` tables, which have no `tenant_id` at all.
The move raises the isolation floor.

**Compatibility.** The read surface is untouched: the same columns with the
same meanings. That is deliberate and is why a cutover of this size is
attemptable.

### Rejected alternatives

**Keep Sub's native implementation and leave the package unconsumed.**
Rejected: the package was extracted FROM Sub as the fleet's conversation owner,
and an unconsumed extraction is an unproven one. It also leaves Sub's three
defects — no tenancy, globally scoped message ids, name-driven channel
behaviour — permanently unfixed.

**Move the whole of `team_inbox.py` into the package.** Rejected: the module
deliberately excludes AI, SLA, media, participants, campaigns, leads and ticket
handoff, and pushing them in would make a shared owner carry one product's
contact-centre policy. Looking alike is not grounds for harvesting
(starter ADR-0006).

**Dual-write Sub and the module indefinitely.** Rejected: two authorities by
construction, with no answer to "which one is right". The shadow phase uses a
bounded, dated dual write and the switch ends it; that is different from
adopting one as the design.

**Retire `public.inbox_*` entirely and repoint every reader.** Correct in the
long run, rejected as a prerequisite: it would block the authority move behind
unrelated work. The projection is explicitly transitional and its retirement is
a later, separate decision.

**Declare `field_job` with the traits the module accepts.** Rejected: it merges
every work order for one subscriber. See § 5.

## Migration and cutover

- **Old owner and paths:** `communications.team_inbox_threads`
  (`app/services/team_inbox_receive.py`), `communications.team_inbox_status`,
  `communications.team_inbox_operator_state`.
- **New owner and paths:** `dotmac_inbox.service`, reached only through one Sub
  adapter package; a reconciler owns the projected columns.
- **Backfill/repair:** `dotmac_inbox.history` under the identity rule.
  Restartable, re-runnable to convergence, refusing rather than defaulting.
  Repair is the same code path as backfill. **Blocked on `dotmac-inbox 0.1.0a2`
  being published** — the history seam does not exist in `0.1.0a1`.
- **Shadow or verification phase:** dual write behind a sealed switch plus a
  per-field comparator. Account-scoped message-identity differences are
  pre-classified and counted; anything unclassified is drift. The Integrator
  `messaging.receive.v1` mirror remains the separate transport proof.
- **Cutover gate and evidence:** (a) census converged, zero refusals, zero
  collisions; (b) comparator clean for a full business cycle including a
  weekend; (c) all four module gaps released and pinned; (d) Sub single-headed
  with `ib` applied; (e) the projected-column writer baseline empty, counting
  constructors as well as field assignments; (f) a PostgreSQL cross-tenant RLS
  canary on all three module tables.
- **Fallback retirement:** local writers removed after the comparator has been
  clean with the module as sole writer, and the Sub tables narrowed to their
  Sub-owned columns in the same change, so a re-introduced local writer fails
  to import rather than silently forking authority.
- **Schema contract step:** additive only until retirement. This change adds
  `mod_inbox` and nothing else.

## Verification

- **Architecture (landed here):**
  `tests/architecture/test_inbox_module_composition.py` — one importer, one
  submodule, no `app/` dependency on the declaration, plus a sensitivity check
  that the scanner reads what it claims to.
  `tests/architecture/test_composed_module_lineages.py` and
  `test_commercial_module_prerequisites.py` (generic, already present) — pin,
  lineage, prerequisite binding and schema contract agree.
- **Behaviour (landed here):**
  `tests/test_inbox_channel_declarations.py` — every declared trait matches the
  design's table; every vocabulary member is declared or named undeclared, with
  no third state; the `field_job` exclusion states a premise the module still
  enforces, and the available alternative demonstrably merges two work orders;
  provider channels thread on the supplied identity and derived channels on the
  contact; every channel threads per connected account; email identity stays
  global while account-scoped channels admit a repeated provider id.
- **Later phases:** the census; backfill replay and refusal tests; reconciler
  idempotence and totality; a migration rehearsal from the deployed revision
  leaving Sub single-headed with `ib` applied; cross-tenant RLS canaries; a
  drift metric and a staleness alert.

## Rollback or forward-fix

This change is reversible by removing three declarations and the two new files:
`mod_inbox` is empty and nothing reads it. Phases through the backfill remain
reversible with no data consequence, because Sub is still the authority and the
module rows are additive.

The writer switch is the point of no easy return: rolling back after it means
the module rows written since have no Sub-side original, so recovery is the
reconciler run in REVERSE. That direction is therefore built and tested in the
shadow phase rather than written under pressure.

Retirement is not reversible without a restore, which is why it is a separate
change gated on a clean comparator with the module as sole writer.

## Review and retirement

- Review date: at the census result, or 2026-11-30, whichever is first.
- Retirement condition: superseded when `public.inbox_conversations` and
  `public.inbox_messages` are retired entirely and their readers repointed at
  the module, which this ADR explicitly does not attempt.
- Supersedes or is superseded by: extends ADR-0011 (module lineage composition)
  and ADR-0009 (operator-tenant bridge). Carries forward the unmerged draft on
  `archive/worktrees/2026-08-25/sub-inbox-cutover`, narrowed to conversations.
  Supersedes nothing.
