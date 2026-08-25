# Sub `dotmac-billing` adoption candidate dossier

**As of:** 2026-08-18
**State:** S0/S1 preparation; no production composition or authority switch

## Candidate identity

- Sub base: `a9da920926a9d9212a8cf03a4744b48a1d4e14f2`
- Billing package contract: `dotmac-billing==0.1.0a1`
- Kernel floor: `dotmac-kernel==0.1.0a69`
- Billing source revision: must be replaced with the exact focused Starter
  commit before this dossier is committed
- Sub adoption source revision: represented by this branch and recorded in the
  Starter extraction dossier after this focused commit is created

Read-only source-evidence pins:

- Vendor CP: `f8f8c3fd636e663e4a17275c19e82fc1667aa52a`
- ERP: `2749ec5396cbbd7a1132b394e85855a1d133a7cd`
- Integrator: `35167813c83ab0ec29c683259ad31479503d812f`
- Durable Timers Starter: `7e0543004864845f0035c9ec325e3f5064c281cc`
- Durable Timers Sub: `4489ca1712f3c263d914f2af0ebfcf044aa70605`
- released kernel relay evidence: `dotmac-kernel==0.1.0a67`,
  `outbox_relay.v1`

## Disposition

The Billing extraction remains greenfield-after-inventory. Sub contributes
proven scenarios and negative tests, not its shadow/dead commercial owners.
Vendor CP has no pre-existing invoice or receivable writer and is the platform
plane first adopter. Sub is the tenant-plane second adopter and requires
backfill, isolated shadow comparison, coupled cutover, and retirement ratchets.

## What this branch owns

- immutable typed source-disposition evidence;
- typed Sub-to-Billing command mapping;
- a tenant-only isolated migration graph;
- route-less/delivery-less target shadow execution;
- exact three-way reconciliation and customer-impact review gates;
- coupled watermark readiness and rollback/roll-forward decisions;
- two-directional measurement of every displaced authority family;
- operator runbook and PostgreSQL tenant-plane proofs.

It owns no financial fact and is not imported by the production application.

## Current verified evidence

- exact pins are machine-checked and no path dependency/lock is fabricated;
- ruff and strict mypy pass for the standalone package and tests;
- focused unit tests cover typed mapping, classification, exact
  reconciliation, topology refusal, coupled readiness, and module selection;
- a fresh real kernel+Billing graph reaches the exact two heads;
- PostgreSQL proves every installed Billing table is tenant-declared, UUID
  `NOT NULL`, ENABLE+FORCE RLS, policy-covered, and exactly granted;
- two-tenant reads/writes and platform-role access fail in the required
  direction;
- the six-category retirement ratchet matches its exact current baseline and
  has a sensitivity proof.

## Gates still deliberately open

- publish/install the exact kernel and Billing candidates from the private
  registry and commit the generated lock;
- classify the real complete source cohort on an approved snapshot;
- run three complete shadow reconciliation windows;
- obtain Finance/product review for any customer-impacting accepted drift;
- record the Integrator checkpoint and coupled source watermark;
- separately authorize production deployment and the authority switch;
- lower the retirement baseline only as legacy writers are actually deleted.

Durable Timers is required before recurring occurrence activation, but not for
the Billing financial engine, Vendor platform adoption, or this S0/S1 shadow
preparation. The timer worktrees remain external read-only dependencies.
