# SOT Pipeline Classification

**Status:** DRAFT — proposed, not approved. Needs Michael's decision on the
open questions in the last section before anything here is normative.
**Scope:** `dotmac_sub`, against the 324 `SOTService` entries in
`app/services/sot_relationships.py` across 26 namespaces.
**Companion to:** `docs/SOT_RELATIONSHIP_MAP.md` (the contracted ownership
manifest, which this classifies rather than replaces).

## Why this exists

The map names owners well: every business decision has one. What it does not
say is which owners form a **chain** and which merely hold a fact. That gap
lets cross-chain state be copied instead of read, which is a distinct and
recurring defect class — see "What this catches" below.

Three different kinds of contract are currently all called "SOT":

| Kind | Governs | Example today |
| --- | --- | --- |
| **Pipeline** | a durable object's irreversible lifecycle | `SALES_TO_SERVICE_LIFECYCLE_SOT.md` |
| **Handoff** | one boundary between two pipelines | `TICKET_WORK_ORDER_HANDOFF_SOT.md` |
| **Ownership map** | who owns which facts inside a domain | `PARTY_ROLE_RELATIONSHIP_SOT.md` |

Same suffix, three different jobs. Labelling them makes the corpus navigable
and stops a projection contract being read as a lifecycle.

## The test

A namespace is a **pipeline** only if all four hold:

1. There is a durable object with identity that outlives any single operation.
2. It has **terminal states that are irreversible** — you cannot un-fulfil,
   un-provision, un-write-off, un-decommission.
3. It crosses more than one owner.
4. It has its own evidence trail and its own reconciler.

Fails (3) → it is a stage inside someone else's pipeline.
Fails (2) → it accumulates rather than progresses; it is a projection.
Fails (1) → it is a control plane or a transport.

**Namespace size is not pipeline count.** `network` (55 services) contains an
asset lifecycle and an outage lifecycle. `financial` (52) contains the money
pipeline and several of its control planes.

## The seven pipelines

| # | Pipeline | Durable object | Terminal states | Principal owners |
| --- | --- | --- | --- | --- |
| 1 | **Party / identity** | Party | merged, departed | `party.*` |
| 2 | **Sale** | Lead → Quote → SalesOrder | fulfilled, cancelled | `sales.*`, `referrals.*` |
| 3 | **Money** | Invoice, Payment | settled, written-off, void | `financial.*` (ledger, invoices, payments, collections, dunning, prepaid) |
| 4 | **Delivery** | Project → WorkOrder | verified / as-built accepted | `operations.*` (project, work order, vendor project) |
| 5 | **Service** | ServiceOrder → Subscription → access | terminated | `service_intent.*`, `access.*`, `operations.service_order_lifecycle`, `operations.provisioning_lifecycle` |
| 6 | **Support** | Ticket | resolved, closed | `support.*` |
| 7 | **Network asset** | NAS, ONT, fibre plant, splitter | decommissioned | `network.*` (inventory, fiber, ont, device state) |

### Boundaries between them

Seven pipelines do not produce twenty-one handoffs. The real ones:

| Handoff | Carries | Contract today |
| --- | --- | --- |
| Party → all | reviewed identity, the spine every other chain binds to | `PARTY_ROLE_RELATIONSHIP_SOT.md` |
| Sale → Money | what is owed for an agreed sale | **none — currently a copied column** |
| Sale → Delivery | authorized implementation scope | `SALES_TO_SERVICE_LIFECYCLE_SOT.md` |
| Delivery → Service | verified implementation releases provisioning | `SALES_TO_SERVICE_LIFECYCLE_SOT.md` |
| Service → Money | active service becomes billable | `adr/0003-permanent-customer-financial-lifecycle.md` |
| Support → Delivery | a fault becomes field work | `TICKET_WORK_ORDER_HANDOFF_SOT.md` |
| Network asset → Service | plant availability constrains delivery | partial (`FIBER_TOPOLOGY_SOT.md`) |

## Everything else

These have owners. They do not have lifecycles, and nothing should treat them
as a chain.

### Projections — derived read models, no terminal state

| Namespace | Note |
| --- | --- |
| `ui.*` (31) | Entirely projection. Already governed by the UI Projection Boundary |
| `observability.*` (6) | Instrumentation over other pipelines' facts |
| `subscriber.growth_reports` (1) | Reporting projection |
| `customer.*` (partial) | `financial_position`, `network_context`, `service_status`, `usage_summary`, `data_completeness` |
| `communications.*` (partial) | Notification and delivery projections of pipeline facts |

### Transports — carry facts between owners, decide nothing

| Namespace | Note |
| --- | --- |
| `integration.*` (10) | Inbox, delivery, registry, ERP adapters |
| `events.*` (2) | Dispatcher and store |
| `app_sessions.*` (3) | Session storage and auth plumbing |

### Control planes — decide configuration, not business state

| Namespace | Note |
| --- | --- |
| `control.*` (6) | Settings, feature registry, module manager |
| `secrets.*` (8) | Credential material and rotation |
| `auth.*` (9) | Permission gate, RBAC catalog, token signing |
| `scheduler.*` (3) | Task registry and worker control |
| `runtime.*` (6) | Health, polling, idempotency |
| `ai.*` (2) | Advisory only; never authoritative over a domain fact |
| `vpn.*` (4) | Remote-access infrastructure |
| `gis.*` (2) | Geocoding and spatial enrichment |

### Observation collectors — write facts, derive nothing

`sessions.*` (4) and the observation half of `network.*`
(`ont_runtime_status`, `ont_topology_observations`, `connection_health`,
`fiber_field_observations`) record what was seen. They are inputs to a
pipeline's resolver, never the resolver.

## What this catches

The classification is not bookkeeping; it is a standing test:

> State belonging to pipeline A must not be **stored** inside pipeline B's
> object. B reads it across the boundary, or asks A to change it.

Applied to the sales work completed in July 2026, this would have caught the
defect class at design time. `SalesOrder` stores `amount_paid`,
`balance_due`, `payment_status` and `paid_at` — Money-pipeline facts held as
Sale-pipeline columns, derived by ad-hoc assignment rather than by the
invoice state machine (`ALLOWED_INVOICE_TRANSITIONS`). One duplicated
boundary produced four separate money bugs:

- a waiver silently revoked by a totals recalculation;
- a line discount restored to gross price by an unrelated edit;
- `amount_paid` inferred from `total` and posted to the ledger unevidenced;
- a second deposit overwriting the first instead of accumulating.

Each was fixed individually. None would have existed had the boundary been a
read instead of a copy.

**Sale owns the price. Money owns the money.** `subtotal`, `tax_total`,
`total` and line discounts are the commercial agreement and belong to the
Sale. `amount_paid`, `balance_due`, `payment_status` and `paid_at` are ledger
facts and belong to Money.

## Findings the classification surfaces

1. **`customer.*` is a grab bag.** Twenty services spanning three kinds:
   pipeline stages (`experience_handoff`, `experience_lifecycle`, belonging to
   Sale/Service), projections (`financial_position`, `service_status`), and
   Party-adjacent commands (`accounts`, `profile_commands`). It should be
   decomposed into the pipelines it actually serves.
2. **Two owners named `subscription_lifecycle`.** `access.subscription_lifecycle`
   and `service_intent.subscription_lifecycle` both exist. Inside one pipeline
   that is an ambiguity, not a layering.
3. **`financial.*` mixes the Money pipeline with its control planes.**
   `payment_routing`, `tax_configuration`, `grace_policy` and
   `payment_configuration_staff_actions` decide configuration; the rest move
   money.
4. **`operations.*` mixes Delivery with Service.** `service_order_lifecycle`
   and `provisioning_lifecycle` advance the Service pipeline while living in
   the Delivery namespace.
5. **Sale → Money has no handoff contract at all** — the one boundary that is
   currently a copied column rather than a documented read.

## Open questions — need a decision

- **Is Outage a pipeline?** `network.outage_lifecycle`, `outage_impact` and
  `outage_auto_notify` have a durable object, a terminal state (resolved) and
  cross owners (network → communications → support). It is the one genuine
  judgement call. Folding it into Network asset keeps the count at seven;
  promoting it makes eight. It advances on staff input rather than purely
  deriving from device state, which argues for promoting it.
- **Is Team Inbox a pipeline?** `communications.team_inbox_*` is 18 services
  with threads, routing, processing and a handoff to tickets. It behaves like
  an intake chain feeding Support. Classified here as projection + transport;
  that may be wrong.
- **Does `auth.*` split?** `permission_gate`, `rbac_catalog` and
  `token_signing` are clearly control plane, but
  `customer_credential_enrollment` and `reseller_onboarding` look like Party
  pipeline stages.
- **Adopt the doc-kind labels?** Adding `kind: pipeline | handoff |
  ownership-map` frontmatter to the 21 existing SOT documents is a mechanical
  pass, but it changes how the corpus is read.

## If adopted

1. Label the 21 SOT documents by kind.
2. Record the seven (or eight) pipelines in `SOT_RELATIONSHIP_MAP.md` and
   classify every namespace that is not one.
3. Write the missing Sale → Money handoff contract, and migrate the sales
   money columns to reads behind it — explicit authority migration with old
   owner, new owner, shadow phase, cutover gate, and boundary tests.
4. Add an architecture test asserting no pipeline object stores another
   pipeline's state, seeded from this classification.
