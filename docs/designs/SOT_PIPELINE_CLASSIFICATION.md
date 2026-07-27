# SOT Pipeline Classification

**doc_kind:** design
**status:** proposed
**authority:** none until approved; `SOT_RELATIONSHIP_MAP.md` remains the
contracted ownership manifest.

**Classification base:** `origin/main` — 324 distinct `SOTService` owners across
26 namespaces in `app/services/sot_relationships.py`. Deliberately not a feature
branch: `feat/external-connector-runtime` still carries the old Team Inbox
catch-all, whereas main carries the decomposed owner family and the
conversation-to-ticket handoff. Classify against main.

## Why this exists

The map names owners well: every business decision has one. What it does not say
is which owners form a **chain** and which merely hold a fact. That gap lets
cross-chain state be copied instead of read — a distinct, recurring defect class
(see "What this catches").

## Three independent axes

A namespace does not determine placement. Each owner is classified on three axes
that vary independently:

| Axis | Question | Values |
| --- | --- | --- |
| **Owning domain** | which service writes this fact | the namespace, e.g. `auth`, `sales` |
| **Manifest role** | what kind of thing it is | pipeline, handoff, workflow hub, projection, transport, control plane, observation collector |
| **Journey position** | where a customer or operator meets it | acquisition, onboarding, delivery, service, billing, support, incident |

`auth.customer_credential_enrollment` is the worked example: owning domain
`auth`, manifest role *handoff*, journey position *onboarding*. Reading any one
axis alone places it wrongly.

## The pipeline test

A candidate is a **pipeline** only if all four hold:

1. A durable object with identity that outlives any single operation.
2. **Irreversible terminal states** — you cannot un-fulfil, un-provision,
   un-write-off, un-decommission, un-resolve.
3. It crosses more than one owner.
4. It has its own evidence trail and its own reconciler.

Fails (3) → a stage inside another pipeline. Fails (2) → it accumulates rather
than progresses; a projection. Fails (1) → control plane or transport.

**Namespace size is not pipeline count.** `network` (55) holds an asset
lifecycle *and* the incident lifecycle. `financial` (52) holds the money
pipeline *and* several of its control planes.

## The eight pipelines

| # | Pipeline | Durable object | Terminal states | Principal owners |
| --- | --- | --- | --- | --- |
| 1 | **Party / identity** | Party | merged, departed | `party.*` |
| 2 | **Sale** | Lead → Quote → SalesOrder | fulfilled, cancelled | `sales.*`, `referrals.*` |
| 3 | **Money** | Invoice, Payment | settled, written-off, void | `financial.*` (ledger, invoices, payments, collections, dunning, prepaid) |
| 4 | **Delivery** | Project → WorkOrder | verified / as-built accepted | `operations.*` (project, work order, vendor project) |
| 5 | **Service** | ServiceOrder → Subscription → access | terminated | `service_intent.*`, `access.*`, `operations.service_order_lifecycle`, `operations.provisioning_lifecycle` |
| 6 | **Support** | Ticket | resolved, closed | `support.*` |
| 7 | **Network asset** | NAS, ONT, fibre plant, splitter | decommissioned | `network.*` (inventory, fiber, ont, device state) |
| 8 | **Outage / incident** | Outage | resolved | `network.outage_lifecycle`, `outage_impact`, `outage_auto_notify` |

### Why Outage is its own pipeline

The incident is an outcome-bearing durable object with explicit closure.
Recovery completes the network incident even if individual support exceptions
remain open — the incident's terminal state is not derived from, and does not
wait on, the Support pipeline. It advances on staff decision as well as observed
device state, so it is not a projection of Network asset either.

### Boundaries between them

Eight pipelines do not produce twenty-eight handoffs. The real ones:

| Handoff | Carries | Contract today |
| --- | --- | --- |
| Party → all | reviewed identity, the spine every chain binds to | `PARTY_ROLE_RELATIONSHIP_SOT.md` |
| Party → credential | account conversion enables credential enrollment | `auth.customer_credential_enrollment` (see below) |
| Sale → Money | what is owed for an agreed sale | **none — currently a copied column** |
| Sale → Delivery | authorized implementation scope | `SALES_TO_SERVICE_LIFECYCLE_SOT.md` |
| Delivery → Service | verified implementation releases provisioning | `SALES_TO_SERVICE_LIFECYCLE_SOT.md` |
| Service → Money | active service becomes billable | `adr/0003-permanent-customer-financial-lifecycle.md` |
| Support → Delivery | a fault becomes field work | `TICKET_WORK_ORDER_HANDOFF_SOT.md` |
| Network asset → Service | plant availability constrains delivery | partial (`FIBER_TOPOLOGY_SOT.md`) |
| Outage → Support | an incident raises customer exceptions | partial (`outage_auto_notify`) |
| Team Inbox → Support | a conversation issues a ticket | `communications.conversation_ticket_handoff` |

## Operational workflow hub

A distinct manifest role, not a weaker pipeline.

**Team Inbox** — `communications.team_inbox_*` (17 owners) plus
`communications.conversation_ticket_handoff`.

It owns real state: conversation, routing, assignment, collaboration and
resolution. It is not a pipeline because **closing a conversation does not
resolve the underlying billing, network, sales or support case**. Ticket
issuance is an independent one-to-many handoff, not the conversation's next
lifecycle state — one conversation may raise several tickets, or none, and the
conversation can close while every ticket it raised stays open.

A workflow hub therefore:

- owns its own working state and may be reconciled;
- must never be read as the authority on any case it touches;
- reaches other pipelines only through declared handoffs.

## Everything else

These have owners. None is a chain.

### Projections — derived read models, no terminal state

| Namespace | Note |
| --- | --- |
| `ui.*` (31) | Entirely projection; already governed by the UI Projection Boundary |
| `observability.*` (6) | Instrumentation over other pipelines' facts |
| `subscriber.growth_reports` (1) | Reporting projection |
| `customer.*` (partial) | `financial_position`, `network_context`, `service_status`, `usage_summary`, `data_completeness` |
| `communications.*` (partial) | Notification and delivery projections, outside the Team Inbox hub |

### Transports — carry facts between owners, decide nothing

`integration.*` (10) · `events.*` (2) · `app_sessions.*` (3)

### Control planes — decide configuration, not business state

`control.*` (6) · `secrets.*` (8) · `scheduler.*` (3) · `runtime.*` (6) ·
`ai.*` (3, advisory only — never authoritative over a domain fact) ·
`vpn.*` (4) · `gis.*` (2) · and the control-plane half of `auth.*` below.

### Observation collectors — write facts, derive nothing

`sessions.*` (4) and the observing half of `network.*`
(`ont_runtime_status`, `ont_topology_observations`, `connection_health`,
`fiber_field_observations`). Inputs to a resolver, never the resolver.

## `auth.*` split by concern

Namespace alone does not determine corpus placement.

| Owner | Owning domain | Manifest role | Journey position |
| --- | --- | --- | --- |
| `auth.permission_gate` | auth | control plane | — |
| `auth.rbac_catalog` | auth | control plane | — |
| `auth.token_signing` | auth | control plane | — |
| `auth.staff_provisioning` | auth | control plane | — |
| `auth.subscriber_assignments` | auth | control plane | — |
| `auth.system_user_assignments` | auth | control plane | — |
| `auth.credential_recovery` | auth | control plane | support |
| `auth.customer_credential_enrollment` | auth | **handoff** | onboarding |
| `auth.reseller_onboarding` | auth | **coordinator** | onboarding (reseller) |

**`auth.customer_credential_enrollment`** is an identity/credential lifecycle
shown as a handoff **following** Party/account conversion — explicitly *not* a
Party-owned stage. It cannot activate a Party, verify Party contact points, or
change account or subscription state. It consumes the conversion outcome; it
does not advance the Party pipeline.

**`auth.reseller_onboarding`** is a cross-domain onboarding **coordinator**, not
a control plane. It belongs in the reseller/Party journey view while **retaining
its current owning service** until an intentional contract migration says
otherwise. Placement in a journey view is not a transfer of ownership.

## What this catches

The classification is a standing test:

> State belonging to pipeline A must not be **stored** inside pipeline B's
> object. B reads it across the boundary, or asks A to change it.

Applied to the sales work of July 2026, this catches the defect class at design
time. `SalesOrder` stores `amount_paid`, `balance_due`, `payment_status` and
`paid_at` — Money-pipeline facts held as Sale-pipeline columns, derived by ad-hoc
assignment rather than by the invoice state machine
(`ALLOWED_INVOICE_TRANSITIONS`). One duplicated boundary produced four money
bugs:

- a waiver silently revoked by a totals recalculation;
- a line discount restored to gross price by an unrelated edit;
- `amount_paid` inferred from `total` and posted to the ledger unevidenced;
- a second deposit overwriting the first instead of accumulating.

Each was fixed individually. None would have existed had the boundary been a
read instead of a copy.

**Sale owns the price. Money owns the money.** `subtotal`, `tax_total`, `total`
and line discounts are the commercial agreement. `amount_paid`, `balance_due`,
`payment_status` and `paid_at` are ledger facts.

## Document metadata

`doc_kind`, `status` and `authority` are independent. A design document can be
approved without being normative; a normative document can be superseded.

**doc_kind** — what the document is:

| Value | Meaning |
| --- | --- |
| `normative` | states rules that bind implementation |
| `decision` | records a choice and its rationale (ADR) |
| `design` | proposes a shape; not binding until adopted |
| `runbook` | operational procedure |
| `audit` | findings from an inspection at a point in time |
| `reference` | descriptive map of what exists |
| `generated` | produced by tooling; edit the source, not the file |

**status** — where it is in its life: `proposed`, `approved`, `implemented`,
`superseded`, `historical`.

**authority** — what it binds, if anything. Absent means it binds nothing.

These prevent proposals, audits and historical plans from reading like active
authority. Relabelling the existing SOT corpus is mechanical **only after these
semantics are fixed** — the vocabulary is the decision; applying it is typing.

## Findings

1. **`customer.*` is a grab bag.** Twenty owners spanning pipeline stages
   (`experience_handoff`, `experience_lifecycle`), projections
   (`financial_position`, `service_status`) and Party-adjacent commands
   (`accounts`, `profile_commands`). Decompose into the pipelines it serves.
2. **Two owners named `subscription_lifecycle`** — `access.*` and
   `service_intent.*`. Inside one pipeline that is ambiguity, not layering.
3. **`financial.*` mixes the Money pipeline with its control planes** —
   `payment_routing`, `tax_configuration`, `grace_policy`,
   `payment_configuration_staff_actions` decide configuration; the rest move
   money.
4. **`operations.*` mixes Delivery with Service** — `service_order_lifecycle`
   and `provisioning_lifecycle` advance Service from the Delivery namespace.
5. **Sale → Money has no handoff contract** — the one boundary that is a copied
   column rather than a documented read.

## If adopted

1. Fix the `doc_kind` / `status` / `authority` semantics above, then relabel the
   21 SOT documents.
2. Record the eight pipelines in `SOT_RELATIONSHIP_MAP.md` and classify every
   namespace that is not one, on all three axes.
3. Write the missing Sale → Money handoff contract and migrate the sales money
   columns to reads behind it — explicit authority migration with old owner, new
   owner, shadow phase, cutover gate and boundary tests.
4. Add an architecture test asserting no pipeline object stores another
   pipeline's state, seeded from this classification.
