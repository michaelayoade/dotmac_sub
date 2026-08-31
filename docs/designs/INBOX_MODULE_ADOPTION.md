# Inbox module adoption: Sub and `dotmac-inbox`

- **Status:** design; decision recorded in
  [`docs/adr/0013-inbox-conversation-authority.md`](../adr/0013-inbox-conversation-authority.md)
- **Date:** 2026-08-30
- **Scope:** the conversation aggregate only — thread identity, ordered
  messages, lifecycle, per-operator read cursors. Queues, routing, presence and
  assignment belong to the sibling `dotmac-inbox-operations` package and are
  **out of scope for this document**; adopting them is a separate decision with
  its own gates.
- **Landed by this change:** composition and channel declarations only. No data
  is migrated, no writer moves, no runtime path changes.

## Why adopt at all

`dotmac-inbox` is a product-first extraction whose qualifying source is Sub
itself (starter `docs/inventories/inbox-sources.md`, ADR-0052). Sub is named
cutover 1 and the package has zero consumers, which means the extraction is
currently unproven: a shared owner nobody consumes is a design document with a
version number.

Three things Sub gets that are not "the same code in a different place":

1. **Tenancy.** Sub's `inbox_conversations`, `inbox_messages` and
   `inbox_conversation_read_states` have no `tenant_id` and no RLS. The module's
   tables are `tenant_id NOT NULL` with forced RLS. The move raises the
   isolation floor.
2. **A canonical thread key and message key**, computed by one function instead
   of being re-decided in each receive service.
3. **Per-channel message identity.** Sub's
   `uq_inbox_messages_inbound_external` is `(channel_type, external_message_id)`
   globally. That is right for email and wrong for every account-scoped
   provider id, and it silently drops legitimate messages today.

## 1. Model reconciliation

### 1.1 `public.inbox_conversations` → `mod_inbox.conversations`

Sub has 18 columns; the module has 17. Only eight are a clean two-way map.

| Sub column | Module column | Verdict |
| --- | --- | --- |
| `id` | `id` | **Same UUID, forever.** See § 3.1. |
| `channel_type` `String(40)` | `channel` `String(40)` | clean |
| `status` `String(40)` | `status` `String(16)` | clean — the four values are identical in both (`open`, `pending`, `snoozed`, `resolved`) |
| `subject` `String(200)` | `subject` `String(255)` | clean, widening |
| `external_thread_id` `String(255)` | `transport_thread_ref` `String(255)` | clean, renamed |
| `first_message_at` / `last_message_at` | same | clean |
| `snoozed_until` | `snoozed_until` | **not clean** — see § 1.4 (indefinite snooze) |
| `contact_address` NULLABLE | `contact` **NOT NULL** | **not clean** — see § 1.4 |
| — | `tenant_id` **NOT NULL** | Sub has none. Derived: `operator_tenant.OPERATOR_TENANT_ID` (ADR-0009) |
| — | `account_scope` **NOT NULL** | Sub has none. Derived; see § 3.3 |
| — | `thread_key` **NOT NULL, unique per tenant** | Sub has none, and has never enforced the uniqueness. See § 3.3 |
| — | `status_reason` | Sub stores no reason today. This is the module's declared reason layer and is where `resolved_to_ticket` belongs |
| — | `tags` JSON | Sub keeps tags inside `metadata_`; the move is a projection decision, not a schema gap |
| — | `resolved_at` | Sub has no column; derivable from `inbox_status_transition_events` |
| `subscriber_id` | — | **stays Sub** |
| `primary_service_team_id` | — | **stays Sub** |
| `priority`, `is_muted`, `is_active` | — | **stays Sub** |
| `metadata_` (44 write sites) | — | **stays Sub** |
| `continued_from_conversation_id` | — | **stays Sub**; see § 1.5 |

### 1.2 `public.inbox_messages` → `mod_inbox.messages`

| Sub column | Module column | Verdict |
| --- | --- | --- |
| `id`, `conversation_id` | same | same UUIDs |
| `channel_type` | `channel` | clean |
| `direction` | `direction` | clean — `inbound`/`outbound`/`internal` identical |
| `subject`, `body` | same | clean |
| `external_message_id` | `transport_message_ref` | clean, renamed |
| `sent_at` + `received_at` | `occurred_at` **NOT NULL** | **narrowing** — two nullable times become one required instant. See § 1.4 |
| — | `message_key` **NOT NULL**, unique per tenant | derived by `dedup_key`; Sub's equivalent is a partial unique INDEX over inbound rows only |
| — | `tenant_id` | operator tenant |
| — | `author_id` UUID nullable | Sub has no author column. See § 1.4 |
| — | `transport_observation_ref` | Sub's `inbox_provider_observations.id`, as an opaque string |
| `from_address`, `to_addresses`, `cc_addresses` | — | **stays Sub** |
| `notification_id` | — | **stays Sub** |
| `external_thread_id` | — | redundant in the module; the conversation carries it |
| `metadata_` | — | **stays Sub** |

### 1.3 `public.inbox_conversation_read_states` → `mod_inbox.conversation_read_states`

The cleanest of the three: `id`, `conversation_id`, `last_read_message_id`,
`last_read_at` map one-to-one, and Sub's `person_id` (deliberately not a foreign
key) is exactly the module's opaque `actor_id`. Nothing is lost in either
direction.

One thing that is *not* this table: the chat-widget VISITOR cursor, which
`team_inbox_widget` keeps as `conversation.metadata_['visitor_last_read_at']`.
It stays Sub's. The module's cursor is a per-OPERATOR cursor; a visitor is the
counterparty, not an operator, and giving the shared table a second kind of
actor would make "who has read this" a question with two answers.

### 1.4 The five facts that do not map, and what each costs

1. **`contact` is NOT NULL.** Sub allows a conversation with no contact
   address: an anonymous widget visitor before they identify, a field-job chat,
   an operator-started internal thread. There is no safe workaround — an empty
   contact puts every anonymous visitor in one derived thread, and a
   synthesised one makes each thread unmergeable when the visitor later
   identifies. **This is a module gap** (`MODULE-GAP-1`, § 6).
2. **Indefinite snooze.** Sub supports "snooze until reply" — `snoozed`
   with no wake time. `dotmac_inbox.lifecycle` requires `snoozed_until` for
   that status. A far-future sentinel was considered and rejected: it reads as
   a real deadline to every report, sort and operator. **`MODULE-GAP-2`.**
3. **Delivery outcome learned late.** Sub stamps `sent_at` and
   `external_message_id` AFTER the provider accepts an outbound message.
   `record_message` consumes the transport ref at admission because it feeds
   the dedup key, so learning it later would mean recomputing `message_key` —
   mutating a unique column other rows have already been deduplicated against.
   **`MODULE-GAP-3`**: the module needs a delivery-outcome operation that
   updates the transport ref without touching the message key.
4. **`occurred_at` is one instant.** Sub's `sent_at`/`received_at` split is a
   product ingress fact (when the provider says it happened vs when we saw it).
   **It does not go into the package.** The module gets
   `coalesce(received_at, sent_at, created_at)`, which is total because
   `created_at` has a default; Sub keeps both columns on its own row.
5. **`author_id`.** Sub has no author column: an operator is in
   `metadata_['actor_id']`, an external party is in `from_address`. Decision:
   `author_id` is the operator's `person_id` for outbound and internal
   messages, and `NULL` for inbound — the counterparty's identity is the
   conversation's `contact`, not a message author, and inventing a UUID for an
   external party would make the column mean two things.

### 1.5 Satellites: what stays in Sub, and why

The test applied to each: *could a second product adopt this concept without
adopting Sub's domain?* If not, it is not shared behaviour.

| Concept | Where it lives now | Decision |
| --- | --- | --- |
| Lead relationships | `inbox_conversation_lead_links`, FK to the conversation | **Sub satellite.** Sales provenance is product logic; the FK survives unchanged because the conversation UUID does not move |
| Ticket handoff | `support_tickets.origin_conversation_id` | **Sub satellite** — the FK is on the ticket, so nothing changes. The one part that DOES enter the module is `resolved_to_ticket` as a declared status **reason**, which is the mechanism ADR-0052 built for exactly this |
| Field-job linkage | conversation `channel_type='field_job'` + `external_thread_id=work_order.public_id` + `metadata_` | **Sub**, and currently **unadoptable** — see § 2.2 |
| AI intake | `ai_intake_sessions` (FK) + `conversation.metadata_['ai_handling']` | **Sub satellite.** Already correctly shaped; the metadata key is a Sub-owned column |
| Participants, contact links, labels, macros, templates, media, comments, saved filters, reply reminders, campaign links, provider observations, delivery receipts | their own Sub tables | **Sub.** Every one either names a Sub identity or rebuilds a transport owner |
| Queues, routing, presence, assignment, round-robin | Sub tables | **Out of scope** — `dotmac-inbox-operations`, separate decision |
| `portal_messages`, `field_job_chat_messages` | dead tables, zero writers | **Neither.** Do not absorb a table nothing writes |

Two refusals worth naming because they look generic and are not:

- **`continued_from_conversation_id`.** Thread continuation reads like a
  conversation-package concept. It is Sub's *policy* about when a finished
  thread becomes a new one; the module already has an answer ("an inbound
  message reopens the resolved thread"). Putting both in the package would give
  one question two owners.
- **`priority` / `is_muted`.** Contact-centre policy. The module orders a queue
  by durable FIFO position on purpose, and a mutable priority is not an answer
  to "where am I in the line".

## 2. Channel declarations

`dotmac_inbox.channels` ships an EMPTY registry. Sub declares its vocabulary in
`app/services/inbox_channels.py`; the traits are transcribed from behaviour and
each is cited in that file.

| Channel | address form | transport | thread identity | message-id scope |
| --- | --- | --- | --- | --- |
| `email` | email | external | provider | **global** |
| `whatsapp` | phone | external | derived | account |
| `website_fiber` | email | external | provider | account |
| `facebook_messenger` | opaque | external | provider | account |
| `instagram_dm` | opaque | external | provider | account |
| `facebook_comment` | opaque | external | provider | account |
| `instagram_comment` | opaque | external | provider | account |
| `chat_widget` | opaque | external | provider | account |
| `note` | opaque | **internal** | derived | none |
| `field_job` | — | — | — | **not declared, see § 2.2** |

### 2.1 The choices that are not obvious

**`email` is the only `GLOBAL` message-id scope.** An RFC 5322 `Message-ID` is
generated to be globally unique, and Sub's existing index already treats it
that way, so declaration and behaviour agree.

**Every other external channel is `ACCOUNT`, and that is a correction.** A
`wamid`, a Messenger message id and a comment id are meaningful only inside the
business account, Page or site they were delivered to. Sub's global index drops
the second arrival at a second connected account today. The declaration admits
it — which means a shadow comparison must **predict and count** that delta
rather than report it as drift.

**`whatsapp` is the only external channel threading on `DERIVED`.** The Cloud
API exposes no thread object; one business number talking to one customer
number is one conversation. Every other external channel carries a thread id on
the message, so it declares `PROVIDER`.

**`chat_widget` is `EXTERNAL`, not `INTERNAL`.** The name suggests a first-party
surface, and the brief that commissioned this design assumed so. The behaviour
disagrees, in three places Sub already wrote down:
`channel_health_contracts.SUPPORTED_EXTERNAL_CHANNELS` includes it,
`team_inbox_observations.InboxProvider.chat_widget` exists, and
`team_inbox_integrator_envelope.PROVIDER_CHANNELS` maps
`chat_widget -> {chat_widget}`. The trait that actually depends on the choice is
thread identity: `team_inbox_widget._thread_id` mints
`chat_widget:{surface}:{entity_id}:{context}` and finds the conversation by it,
which is *richer* than the module's derived `(channel, account, contact)` —
derived would merge a visitor's ticket-scoped chat into their general one. An
internal transport may not claim a provider thread identity, so declaring
`INTERNAL` would force the merge. `EXTERNAL` is both the evidenced and the
correct answer.

That said, `Transport` is doing two jobs — "a third party operates this" and
"identity arrives with the message" — and `chat_widget` is where they come
apart. Recorded as an open question in § 6, not worked around.

**`note` is `INTERNAL`.** No provider, nothing delivered, excluded from
`SUPPORTED_EXTERNAL_CHANNELS` and unreachable from `PROVIDER_CHANNELS`.
`DERIVED` + `NONE` are forced by the trait validation and correct: a note thread
has no id of its own. In practice no live Sub writer creates a `note`
conversation — internal notes are `direction='internal'` messages on the host
conversation's channel — so the declaration exists for the vocabulary member and
any historical rows.

### 2.2 `field_job` is deliberately not declared

`field_job` has no external transport (the enum says so; delivery is the shared
conversation websocket) **and** its thread identity is the work order:
`team_inbox_field_job` sets `external_thread_id = work_order.public_id` and
finds the conversation by it. `ChannelSpec` refuses `Transport.INTERNAL`
together with `ThreadIdentity.PROVIDER`, so the truthful declaration cannot be
constructed. The only constructible alternative, `DERIVED`, keys on
`(channel, account_scope, contact)` — identical for every work order one
subscriber ever has — and would collapse them into a single conversation.

Declaring it wrongly fails silently at cutover. Leaving it undeclared fails
loudly at the first call, because `channel_spec("field_job")` raises
`UnknownChannelError` naming the declaration module. `UNDECLARED_CHANNELS`
records the premise and
`tests/test_inbox_channel_declarations.py` proves the module still enforces it,
so the exclusion expires by itself when the module changes. **`MODULE-GAP-4`.**

## 3. Migrating the existing conversations

Not part of this change. Specified here so the gates are agreed before anyone
writes the backfill.

### 3.1 The identity rule

`mod_inbox.conversations.id` is the SAME UUID as
`public.inbox_conversations.id`, and likewise for messages and read states. The
backfill copies identity verbatim and never mints one.

This is what makes the cutover affordable. Sub tables carry foreign keys to
`inbox_conversations.id`; conversation ids appear in URLs, exports and saved
filters. Keeping the UUID means those stay valid and drift is comparable
row-for-row by primary key rather than through a mapping table that could
itself be wrong.

`dotmac_inbox.service.create_conversation` mints its own id and cannot express
this — correctly, because it is the runtime entry point. The
identity-preserving seam is `dotmac_inbox.history`
(`import_conversation` / `import_message` / `import_read_state`), which
preserves source UUIDs and timestamps, validates the same channel, lifecycle,
threading and identity contracts, replays an exact existing row idempotently,
and fails closed on same-id/different-fact and natural-identity collisions.

**`history` ships in `dotmac-inbox 0.1.0a2`, which is NOT PUBLISHED.** The
starter's `declared-publication-baseline.json` records it as
`declared-unpublished` with the reason naming this adoption, and says Sub must
not pin it until the protected release installs it back and tags the exact
revision. **Publishing a2 is a hard prerequisite for the backfill slice**, and
the alternative — Sub writing the module's mapped classes directly — is a
temporary bridge ADR-0052's amendment permits only while a2 is unpublished, and
is not something to choose when publishing is available.

### 3.2 Expand / contract sequence

| Phase | What happens | Reversible |
| --- | --- | --- |
| **P0 compose** (this change) | pin, lock, `ib` lineage in `alembic.ini`, `mod_inbox` in the schema prerequisite contract, channel declarations | yes — revert three declarations; `mod_inbox` is empty |
| **P1 census** | derive `account_scope` and `thread_key` for every historical conversation against a production-shaped restore; count refusals and collisions; write nothing | yes, nothing written |
| **P2 satellite schema** | additive Sub-owned tables the projection needs, if the census says any are needed | yes |
| **P3 backfill** | `history.import_*` under the identity rule; restartable, re-runnable to convergence | yes — `mod_inbox` rows are additive and Sub is still authoritative |
| **P4 shadow** | writes go to both, behind a sealed switch; comparator reports per-field drift | yes |
| **P5 writer switch** | the module becomes the only writer; Sub's columns become a projection with one writer | this is the point of no easy return |
| **P6 retirement** | drop the retired tables and the projected columns that stop being written | no, without a restore |

### 3.3 Provenance carried by imported rows

- `tenant_id` — `operator_tenant.OPERATOR_TENANT_ID`. Sub is a dedicated
  single-operator deployment (ADR-0009) and already sets this exact value as
  the `app.current_tenant` GUC on every root transaction.
- `account_scope` — resolved by a ladder, first hit wins, **no default**:
  1. `inbox_provider_observations.provider_account_scope` for the observation
     joined to this conversation. Strongest evidence: what the provider said.
  2. `team_inbox_channel_routes.account_scope`, matched on
     `(channel_type, is_active)` where the conversation's primary team matches
     the route's team and exactly one active route qualifies.
  3. For channels declared `Transport.INTERNAL`, the literal `sub:internal`,
     read from `INTERNAL_TRANSPORT_CHANNELS` rather than written out again.
  4. **No fourth rung.** A row that reaches here is refused and reported by id.
- `thread_key` / `message_key` — `dotmac_inbox.threading.thread_key()` and
  `dedup_key()`, never reimplemented in SQL. A second implementation in a
  migration is the exact drift the extraction existed to end.
- `occurred_at` — `coalesce(received_at, sent_at, created_at)`.
- `created_at` / `updated_at` — copied from the Sub row, not stamped now.
- `transport_observation_ref` — `inbox_provider_observations.id` as a string.

`thread_key` is unique per tenant and Sub's data has never been constrained
that way. **The backfill refuses on the first collision** and reports the
colliding set. Merging two conversations because their derived keys agree is a
data decision for a human against a census, not an `ON CONFLICT DO NOTHING`.

### 3.4 Proving idempotency and replay

- Run the backfill twice over the same fixture; the second run writes nothing
  and raises nothing.
- Mutate one field of an imported row and re-run: the run must refuse with the
  field named, not overwrite.
- Import a conversation whose derived `thread_key` collides with an existing
  one: refuse, name both ids.
- Import a message whose derived `message_key` collides: refuse.
- Empty `mod_inbox` and re-run to convergence; compare row-for-row by primary
  key.

### 3.5 Drift detection during the shadow phase

A comparator, run on a schedule, reporting per-field differences between
`public.inbox_*` and `mod_inbox.*` keyed by the shared UUID. Fields compared:
thread key, status and reason, ordered message keys and their order, activity
clocks, and each operator's read cursor.

One class of difference is **expected and must be classified rather than
counted as drift**: account-scoped message identity (§ 2.1) admits messages
Sub's global index dropped. The comparator must report that class separately
with a count; an unclassified difference is drift.

The Integrator `messaging.receive.v1` mirror remains the TRANSPORT cutover
proof. Module parity does not replace it and neither is evidence for the other.

### 3.6 Old owner, new owner, gate, retirement

- **Old owner:** `communications.team_inbox_threads`
  (`app/services/team_inbox_receive.py`) for conversation and message records;
  `communications.team_inbox_status` for status;
  `communications.team_inbox_operator_state` for the read cursor.
- **New owner:** `dotmac_inbox.service`, reached only through a single Sub
  adapter package.
- **Shadow gate:** zero unexplained comparator differences over a full business
  cycle including a weekend, not one green run.
- **Cutover gate:** census converged with zero refusals and zero collisions; a
  PostgreSQL cross-tenant RLS canary on all three module tables; the four
  module gaps in § 6 closed and released; the projected-column writer baseline
  empty; a sealed one-writer switch row.
- **Fallback retirement:** local writers removed in P6, after the comparator
  has been clean with the module as sole writer, and the Sub tables narrowed to
  their Sub-owned columns in the same change so a re-introduced local writer
  fails to import rather than silently forking authority.

## 4. One writer

Today the write surface is already small and mostly named — which is why this
is tractable:

- `inbox_conversations` is INSERTed in 7 places
  (`team_inbox_receive` ×2, `team_inbox_channel_receive`, `team_inbox_commands`,
  `team_inbox_field_job`, `team_inbox_widget`, `team_inbox_campaigns`).
- `inbox_conversations.status` has exactly ONE writer already:
  `team_inbox_status._apply_status_transition`.
- `inbox_messages` is INSERTed in 12 places, dominated by
  `team_inbox_outbound`.
- `inbox_conversation_read_states` has exactly ONE writer:
  `team_inbox_read_state`. No other module in `app/` even references the model.
- There is **no raw SQL** against any of the three tables. Every write is ORM.

After cutover:

- One Sub adapter package is the only code that speaks to `dotmac_inbox`. Every
  runtime write goes through the module's service functions.
- `public.inbox_conversations` and `public.inbox_messages` survive as
  PROJECTIONS. Their columns split into a projected set (mirrors of module
  facts, written **only** by the reconciler) and a Sub-owned set
  (`subscriber_id`, `primary_service_team_id`, `priority`, `is_muted`,
  `is_active`, `metadata_`, `continued_from_conversation_id`, and on messages
  `notification_id`, `from_address`, `to_addresses`, `cc_addresses`,
  `metadata_`). This is what lets the large read surface stay untouched. It is
  a cache with provenance and repair, not a second authority: the reconciler is
  idempotent, rebuilds any row from `mod_inbox` alone, and a drift report is an
  alert rather than a merge.
- `inbox_conversation_read_states` is retired outright rather than projected —
  it has one writer, one reader and no Sub-owned columns.

**What fails if a second writer reappears.** Three executable guards, each
failing on a different shape of mistake:

1. **The import guard, already landed in this change** —
   `tests/architecture/test_inbox_module_composition.py` allows exactly one
   `app/` module to name `dotmac_inbox`, and allows it only
   `dotmac_inbox.channels`. Naming `dotmac_inbox.service` from a second file
   fails immediately.
2. **A projected-column writer ratchet** (P4): an AST census of every
   assignment to a projected column and every `InboxConversation(...)` /
   `InboxMessage(...)` construction outside the reconciler, with a two-
   directional baseline that fails when the count rises OR falls without the
   baseline being lowered. Constructors are counted, not only field
   assignments: a baseline blind to constructors can reach zero while every
   admission path still creates Sub-only rows.
3. **The sealed switch** (P5): a durable, uniquely-keyed, delete-free
   authority row, following `customer_subledger_authority_cutovers`. Exactly
   one module may read the stage; a guard proves no other module branches on
   it.

## 5. The real-time seam

The boundary is already in the right place and must stay there.

`/ws/inbox` (`app/websocket/router.py`) authenticates, registers a connection
and fans events out to topics. It holds no conversation state — the connection
manager is in-memory, and every payload is built at the write site and
published through `team_inbox_realtime.publish_conversation_event`, which
`run_after_commit` defers until the owning transaction has committed.

The rules:

- **`dotmac-inbox` never grows a websocket, a connection manager, a topic or a
  publish call.** It owns conversation state and has no delivery concern.
  Realtime is listed as product-owned in the extraction's own boundary table.
- **Sub's delivery layer keeps no copy of conversation state.** After cutover,
  the payload is built from the `Message` and `Conversation` rows the module's
  service RETURNS, in the same transaction that wrote them, and published
  after commit exactly as today. It must not be built from a cached dictionary
  assembled before the write, because then a rejected or deduplicated write
  would still be broadcast.
- **A deduplicated write publishes nothing new.** `record_message` returns the
  existing row on an exact replay; the publisher must compare identity and stay
  silent, or the same message arrives twice in every open browser.
- **The projection is not the event source.** Events are published from the
  owner's outcome, not from the reconciler, so a stopped reconciler makes
  reads stale without making the live stream wrong.

## 6. Risks, unknowns and module gaps

### Module gaps that block activation (not workarounds — changes `dotmac-inbox` needs)

| id | Gap | Why no workaround |
| --- | --- | --- |
| `MODULE-GAP-1` | No typed internal principal: `contact` is NOT NULL, and Sub has conversations with no external party | empty contact merges every anonymous visitor; a synthesised one is unmergeable when they identify |
| `MODULE-GAP-2` | No indefinite snooze: `snoozed` requires `snoozed_until` | a sentinel timestamp reads as a real deadline everywhere |
| `MODULE-GAP-3` | No delivery-outcome operation: the transport ref is taken at admission because it feeds the dedup key | learning it later means recomputing a unique column other rows deduplicated against |
| `MODULE-GAP-4` | An internal transport may not carry a supplied thread identity, which `field_job` needs | the only constructible alternative merges every work order for one subscriber |

### Unknowns

- **The census has never been run.** How many historical conversations reach
  rung 4 of the `account_scope` ladder, and how many derived `thread_key`s
  collide, is unknown until it is measured against a production-shaped restore.
  No host is named here; naming one is Michael's.
- **Historical `note` conversations.** Whether any rows carry
  `channel_type='note'` is unknown without a database. The declaration covers
  them if they exist.
- **`dotmac-inbox 0.1.0a2` publication.** The backfill cannot start until it is
  published and pinned. Whether it will also carry the four gaps above is a
  starter-side decision.
- **Read surface.** The projection strategy assumes the read surface is large
  enough that repointing it is a separate project. That was measured at ~51
  files on an earlier revision and has not been re-measured here.

### Risks

- **The account-scoped correction changes behaviour before any authority
  moves** — but only once the module is a writer. Until then it is inert.
  During shadow it will produce differences that MUST be pre-classified, or the
  zero-drift gate becomes a judgement call.
- **`mod_inbox` is created by the deploy's own repair leg.** `scripts/deploy.sh`
  verifies the module schema contract with the restricted migration connection
  BEFORE Alembic. Composing the lineage adds `mod_inbox` to the derived
  contract, so the first deploy after composition must repair it. Superseded
  2026-08-31: this used to say an elevated `BOOTSTRAP_DATABASE_URL` bootstrap
  had to be supplied once, which is now refused. The deploy repairs with the
  least-privilege `dotmac_schema_bootstrap` credential and reports
  `already_satisfied`, `repaired` or `blocked` — and a `blocked` deploy stops
  rather than continuing to a verification it cannot pass.
- **Two packages, one domain.** Adopting `dotmac-inbox` without
  `dotmac-inbox-operations` leaves assignment and queueing in Sub. That is a
  coherent boundary — the operations package holds only an opaque
  `conversation_reference` and never a conversation row — but the two cutovers
  interact and should be sequenced deliberately rather than by accident.
