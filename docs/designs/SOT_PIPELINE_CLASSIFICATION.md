# SOT Pipeline Classification

**doc_kind:** design
**status:** proposed
**authority:** none. Nothing here is normative until Michael approves the
taxonomy. `app/services/sot_relationships.py` remains the executable
concern/role owner registry and `docs/SOT_RELATIONSHIP_MAP.md` the generated
relationship map.

**Owner / system of record:** Michael decides the taxonomy. The candidate and
its open findings are tracked in the project memory
`dotmac-sub-pipeline-and-document-taxonomy-candidate`, which is authoritative
for status; this document is the working draft.

**Classification base:** `origin/main` — 324 distinct `SOTService` owners across
26 namespaces. Deliberately not a feature branch:
`feat/external-connector-runtime` still carries the old Team Inbox catch-all,
whereas main carries the decomposed owner family and the conversation-to-ticket
handoff.

## Why this exists

The map names owners well: every business decision has one. What it does not say
is which owners form a **chain** and which merely hold a fact. That gap lets
cross-chain state be copied instead of read — a distinct, recurring defect class
(see "What this catches").

## Three independent axes

A namespace does not determine placement. Classify each owner separately on:

| Axis | Question |
| --- | --- |
| **Owning domain** | which service writes the fact |
| **Manifest role** | pipeline, handoff, bounded operational workflow, projection, transport, control plane, observation collector |
| **End-to-end journey** | where a customer or operator meets it |

`auth.customer_credential_enrollment` is the worked example: owning domain
`auth`, manifest role *handoff*, journey *onboarding*. Reading any one axis
alone places it wrongly.

## The pipeline test

A top-level pipeline has:

1. one durable **work-item spine**;
2. explicit **entry and terminal/closure semantics**;
3. **staff or policy commands that advance it**;
4. **preserved identity and provenance across owner handoffs**;
5. an accountable **end-to-end business or operational outcome**.

**Not pipeline criteria:** service count, module prefix, UI size, or the
presence of a handoff link. `network` (55 owners) holds both an asset lifecycle
and the incident lifecycle; `financial` (52) holds the money pipeline and
several of its control planes. Size indicates decomposition, not boundary.

## The eight pipelines

| # | Pipeline | Work-item spine | Closure | Principal owners |
| --- | --- | --- | --- | --- |
| 1 | **Party / identity** | Party | merged, departed | `party.*` |
| 2 | **Sale** | Lead → Quote → SalesOrder | fulfilled, cancelled | `sales.*`, `referrals.*` |
| 3 | **Money** | Invoice, Payment | settled, written-off, void | `financial.*` (ledger, invoices, payments, collections, dunning, prepaid) |
| 4 | **Delivery** | Project → WorkOrder | verified / as-built accepted | `operations.*` (project, work order, vendor project) |
| 5 | **Service** | ServiceOrder → Subscription → access | terminated | `service_intent.*`, `access.*`, `operations.service_order_lifecycle`, `operations.provisioning_lifecycle` |
| 6 | **Support** | Ticket | resolved, closed | `support.*` |
| 7 | **Network asset** | NAS, ONT, fibre plant, splitter | decommissioned | `network.*` (inventory, fiber, ont, device state) |
| 8 | **Outage / incident** | Outage | suspected → confirmed → clearing → resolved / discarded | `network.outage_lifecycle`, `outage_impact`, `outage_auto_notify` |

### Why Outage is the eighth

`network.outage_lifecycle` owns a persisted incident with an explicit
suspected / confirmed / clearing / resolved / discarded vocabulary.
Authoritative observations **and staff decisions** advance it, so it is not a
projection of asset state. Its consequences cross network impact,
communications and staff notification, SLA, and Support/field verification.
Support tickets may outlive incident resolution without becoming part of the
outage aggregate — the incident closes on recovery regardless.

## Bounded operational workflow

A distinct manifest role, not a weaker pipeline.

**Team Inbox** — `communications.team_inbox_*`, a communications
intake-and-collaboration workspace.

The family owns durable provider observations, conversation and message
chronology, contact resolution, routing, assignment and escalation, operator
state, outbound intents, receipts, commands, repair and projections. **Providers
and realtime remain transports and projections** — the role is not uniform
across the family.

`communications.conversation_ticket_handoff` is an **issuance and provenance
edge**, not a lifecycle transition: one conversation may issue many tickets,
issuance does not transition the conversation, and Support owns the ticket
lifecycle throughout. The 18-service count reflects ownership decomposition, not
a pipeline boundary.

A bounded workflow owns its working state, must never be read as the authority
on any case it touches, and reaches other pipelines only through declared edges.

## Everything else

### Projections — derived read models

`ui.*` (31) · `observability.*` (6) · `subscriber.growth_reports` (1) ·
parts of `customer.*` (`financial_position`, `network_context`,
`service_status`, `usage_summary`, `data_completeness`) · the notification and
delivery projections in `communications.*` outside the Team Inbox workspace.

### Transports — carry facts, decide nothing

`integration.*` (10) · `events.*` (2) · `app_sessions.*` (3)

### Control planes — decide configuration, not business state

`control.*` (6) · `secrets.*` (8) · `scheduler.*` (3) · `runtime.*` (6) ·
`ai.*` (3, advisory only) · `vpn.*` (4) · `gis.*` (2) · the control-plane half
of `auth.*`.

### Observation collectors — write facts, derive nothing

`sessions.*` (4) and the observing half of `network.*`
(`ont_runtime_status`, `ont_topology_observations`, `connection_health`,
`fiber_field_observations`). Inputs to a resolver, never the resolver.

## `auth.*` split by concern

Split in **corpus presentation** by concern rather than namespace. This is a
presentation split; no owner is renamed or moved.

| Owner | Manifest role | Journey |
| --- | --- | --- |
| `permission_gate`, `rbac_catalog`, `token_signing`, `staff_provisioning`, `subscriber_assignments`, `system_user_assignments` | control plane | — |
| `credential_recovery` | control plane | support |
| `customer_credential_enrollment` | **handoff** | onboarding |
| `reseller_onboarding` | **cross-domain application coordinator** | reseller / Party onboarding |

`auth.customer_credential_enrollment` is an identity/credential lifecycle that
**follows** account conversion. It explicitly owns no Party activation, no
contact verification, no account lifecycle and no subscription state. Show it as
a journey handoff while keeping `auth` ownership.

`auth.reseller_onboarding` coordinates reseller/account/principal bootstrap and
belongs in the reseller/Party onboarding journey view. **Its owner namespace
must not be renamed without a deliberate contract migration.**

## Document metadata

`doc_kind` is orthogonal to `status`, authority and supersession. Classification
is **semantic, not purely mechanical** — the label is a judgement about what the
document is for.

- **doc_kind**: normative standards/maps · decisions · designs · runbooks ·
  audits/evidence · references/guides · generated artifacts
- **status**: proposed · approved · implemented · superseded · historical
- **authority**: what it binds; absent means it binds nothing

Labels aid retrieval and stop plans and audits being read as current authority.
They do **not** replace the executable SOT manifest or the checked-in precedence
rules.

## What this catches

> State belonging to pipeline A must not be **stored** inside pipeline B's
> object. B reads it across the boundary, or asks A to change it.

Applied to the sales work of July 2026, this catches the defect class at design
time. `SalesOrder` stores `amount_paid`, `balance_due`, `payment_status` and
`paid_at` — Money-pipeline facts held as Sale-pipeline columns, derived by ad-hoc
assignment rather than by the invoice state machine
(`ALLOWED_INVOICE_TRANSITIONS`). One duplicated boundary produced four money
bugs: a waiver revoked by a totals recalculation; a line discount restored to
gross by an unrelated edit; `amount_paid` inferred from `total` and posted to the
ledger unevidenced; a second deposit overwriting the first. Each was fixed
individually; none would have existed had the boundary been a read.

**Sale owns the price. Money owns the money.**

## Open findings — unresolved, not decided

The five findings below are recorded as **unresolved observations, not approved
renames, moves, mergers or contract changes.** The classification recommendations
in this document do **not** close any of them. They must remain visible whenever
this taxonomy is revised. Each requires separate adjudication.

| # | Finding | Status |
| --- | --- | --- |
| 1 | `customer.*` is a grab bag rather than a coherent ownership/domain boundary | open |
| 2 | Duplicate `subscription_lifecycle` owners (`access.*`, `service_intent.*`) | open |
| 3 | `financial.*` mixes business pipelines with control planes | open |
| 4 | `operations.*` holds Service owners (`service_order_lifecycle`, `provisioning_lifecycle`) | open |
| 5 | Sale → Money has no explicit handoff contract | open |

Reaffirmed unchanged by Michael on 2026-07-27.

## Next action

Michael approves or adjusts the classification recommendations, and
**separately** adjudicates the five open boundary/handoff findings. Only then
encode the resulting pipeline definitions, domain boundaries, handoff contracts
and document-kind/status schema in the canonical corpus documentation — and only
after that relabel documents mechanically.
