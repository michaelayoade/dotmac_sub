# SOT coding standards refactor

Status: implemented

Change classification: major

Owning system: Dotmac Sub

Working branch: `feat/sot-coding-standards-refactor`

Base: `origin/main` at `ec6ee30a725f0f1c7619e3fa7b738d05d71b26ae`

## Baseline at branch creation

- 270 undeclared persistence-writing service modules.
- 351 direct decision-input bypass occurrences: 142 environment reads and 209
  raw setting reads.
- 16 web/API files and 36 task files with direct transaction commits.
- 225 service files containing FastAPI `HTTPException` references; the AST
  inventory confirms 224 files currently use an imported symbol.
- 66 architecture-test modules.

The foundation scanner additionally records 402 transaction operations or
legacy transaction-helper calls across 89 API, web, task, and event-handler
adapter files. The executable registry contains 28 domains and 254 service
entries. Its one exact duplicate concern claim has been split into distinct
observation-evidence and customer-resolution concerns.

Typed manifest migration now has 43 fully contracted services and 211 indexed
legacy services in a shrink-only baseline. New registry services cannot
be added without role-qualified concerns, authoritative inputs, transaction and
error semantics, migration state, stewardship, checked-in evidence, and any
applicable event or projection/repair contract.

The current-main reconciliation contracts the new material-dependency,
ERP-material-support, and vendor-invoice ERP projection owners without growing
the legacy baseline. ERP payment reconciliation depends on the canonical invoice
record owner; vendor reads consume its timestamped observation. This direction
keeps transport, observation, and business-decision authority explicit and
prevents a dependency cycle through UI presentation services.

The architecture suite now has a measured four-worker development gate:
115 seconds versus 241 seconds serial on the foundation worktree (52% faster).
Using all detected workers took 170 seconds and substantially more CPU, so
unbounded `-n auto` is not the local architecture default. Shared scanners are
cached within a worker; broader cross-module indexing remains a future
optimization if the suite grows materially.

The first runtime transaction slice is `network.device_projection`. The typed
reconcile command is admitted through the manifest-verified owner-command
executor, rejects caller and nested transaction ownership, takes a
transaction-scoped PostgreSQL single-flight lock, atomically stages a versioned
outbox event, and commits or rolls back before returning. Its Celery adapter now
owns only non-committing session lifecycle. ADR 0002 makes this the required
runtime pattern for subsequent migrated write owners.

The next completed runtime slice is `auth.staff_provisioning`. ERP staff create,
managed-role convergence, activation/deactivation, credential state, session
revocation, audit evidence, and outbox events now share one coordinator-owned
transaction. Managed role mutation is delegated to the system-user assignment
owner. Invitation delivery is a replay-safe event consequence, and its password
capability is minted only after the worker revalidates the exact staff principal
at transport time.

The completed reseller onboarding slice is `auth.reseller_onboarding`. Reseller
record creation, optional portal identity and credential bootstrap, legacy
subscriber initialization, reseller link, authorized role grant, audit evidence,
and versioned events now share one coordinator-owned transaction. Both legacy
subscriber-backed and first-class `ResellerUser` principals use a deduplicated
communication intent; the worker revalidates the exact reseller/principal/email
binding and mints the reset capability only in memory at delivery time. The old
multi-commit path, compensating deletion, portal-level principal writer, and
synchronous invite call are retired.

The completed credential recovery slice is `auth.credential_recovery`. Public
email requests, exact-principal administrative requests, capability claims and
lifetime, delivery context, credential replacement, session revocation, audit,
and events now have one owner. A request commits only an email digest and exact
principal identifiers to the event/outbox pipeline; the communication worker
revalidates canonical identity and local-credential state before minting the
bearer in memory and placing it in a URL fragment. Redemption locks the exact
principal and credential, rejects expired or spent capabilities, and commits
the password transition, database-session revocation, PII-safe audit, and
versioned event atomically. API and portal adapters map domain errors, and the
legacy synchronous recovery delivery and service-owned HTTP/commit path are
retired from production callers. The completion event also has one idempotent,
retryable session-projection handler for auth-cache invalidation and customer or
reseller portal-session revocation; a cache outage remains visible as a failed
event-handler attempt instead of silently losing the security consequence.

The completed referral credential-enrollment slice is
`auth.customer_credential_enrollment`. Referral delivery requests and
capability redemption now enter typed owner commands on transaction-free
sessions. The request intent, audit, and versioned event commit atomically and
deduplicate by the exact referral; the delivery worker revalidates the
Referral/Party/Lead/Subscriber/email-digest binding and mints the bearer only in
memory. Redemption locks that canonical context and commits the local
credential, Subscriber email verification, audit, and completion event as one
transition. Password minimum, invite lifetime, and bounded request-rate policy
come only from `control.settings_spec`; event replay owns strict authentication
cache repair. Service commits, savepoints, transport-coded errors, duplicate
intents, and best-effort cache invalidation are retired.

The completed referral account-conversion slice is
`referrals.account_conversion`. Public signup, staff account creation, and
reviewed existing-account attachment now enter typed coordinator commands on
transaction-free sessions. The exact Referral/Party/Lead context and selected
Subscriber are locked and revalidated; account initialization, Party binding,
Lead and Referral attachment, PII-free audit, `subscriber.created`, and
`referral_account.converted` commit or roll back together. Exact replay returns
the attached account without duplicate evidence. The public capability lifetime
resolves only from the bounded `control.settings_spec` input, while its purpose,
version, claim allowlist, token size, and clock-skew rules remain protocol and
security invariants. Service commits, savepoints, transport errors, keyword
mutation entry points, hardcoded lifetime, and adapter-owned post-conversion
transaction completion are retired.

The completed referral-program slice is `referrals.program`. Code issuance,
Party-first capture, exact Referral/Party/Lead account attachment,
qualification, rejection, reward issuance, and reward reconciliation now enter
typed coordinator commands on transaction-free sessions. The owner locks the
canonical records, makes exact replays idempotent, and commits Referral state,
PII-free audit evidence, and versioned events together. Reward accounting is
delegated to `financial.credit_notes`; customer notification is an idempotent
event consequence. Program enablement, reward amount and currency,
qualification window, automatic approval, and share base URL resolve only from
`control.settings_spec`. HTTP errors, adapter transactions, direct CRM credit
calls, direct push delivery, environment reads, and legacy keyword mutation
entry points are retired from the owner boundary.

The completed account-adjustment slice is `financial.account_adjustments`.
Direct debit and reversal confirmations now enter typed owner commands on
transaction-free sessions; prepaid plan changes, add-on purchases, and prepaid
service renewals use explicit typed staging collaborators inside their wider
coordinator transactions. Account and adjustment locks, origin-scoped database
uniqueness, exact replay validation, append-only ledger links, audit evidence,
and PII-free versioned events form one atomic boundary. Omitted currency uses
only `control.settings_spec`'s billing default. Structural evidence
inspection detects drift without inventing monetary provenance, while the
billing alignment audit's zero historical adjustment-debit drift is the
cutover verification. Service-owned HTTP errors, conditional commits and
rollbacks, commit flags, and the ambiguous mutation facade are retired.

The completed event-policy slice is `access.event_policy`. Event handlers and
the RADIUS projection now consume typed group-routing, session-refresh, and FUP
policy outcomes. Canonical defaults live only in `control.settings_spec`;
invalid event action evidence, invalid setting values, and missing or malformed
throttle-profile evidence fail with stable domain errors instead of silently
falling back or skipping enforcement. Invoice-overdue events remain
observations, with every consequence owned by financial dunning.

The completed access-resolution slice is `financial.access_resolution`. The
duplicate `access.control_resolution` registry identity is removed, and
`customer_service_state` retains only outage/support observations. Customer
impact, invoice eligibility, prepaid enforcement, funding, and RADIUS callers
now consume one typed decision implementation. Currency validation raises a
stable domain error; no caller compares amounts under a local currency default.

The completed captive-restriction slice is `access.walled_garden_policy`.
Eligibility, explicit opt-in, network readiness, terminal lifecycle state, and
active-lock precedence now produce one typed reason outcome. Hard reject remains
the deterministic result for missing, invalid, stale, or conflicting evidence;
financial, event, RADIUS, connectivity, and status callers share that policy.

The completed billing-profile slice is `financial.billing_profile`. Account,
collectible-subscription, offer, and requested billing-mode evidence now resolve
through typed source and reason contracts with stable domain failures. Generic
account updates, catalog writes, cleanup remediation, collections, access, and
reporting consume the same resolver or transition policy. Cleanup revalidates
the live profile immediately before applying an account-mode alignment, while
grace policy fails closed instead of selecting a caller-local fallback when the
profile is missing, mixed, or contradictory.

The completed prepaid funding-input slice separates
`financial.prepaid_currency` from `financial.access_resolution`, removing the
hidden threshold-to-access-resolution callback cycle. Access, funding position,
threshold, enforcement planning, and readiness now consume one normalized,
fail-closed currency policy. `financial.prepaid_threshold` returns typed minimum
and unfunded-renewal provenance from the batched owner. The duplicate
service-status derivation is removed, and missing accounts, invalid minimums,
unpriced collectible subscriptions, and cross-currency prices are stable domain
failures instead of guessed or silently ignored inputs.

The completed collections grace slice is `financial.grace_policy`. Account,
reseller, offer/version, policy-set, and billing-mode default precedence now
returns typed policy-set provenance, grace provenance, deadline, and phase.
Default policy identifiers resolve only through `control.settings_spec`; the raw
setting-row bypass is retired. Invalid identifiers and negative or malformed day
values are stable domain failures, never silent zero-day grace that can trigger
an immediate financial access consequence. Naive input timestamps are normalized
to UTC before phase decisions. Retiring the direct setting-row query reduces the
decision-input inventory from 350 to 346 occurrences and from 83 to 82 files.

The completed prepaid planning slice is `financial.prepaid_enforcement`. Cohort,
funding-only eligibility, repair inclusion, policy settings, and each account's
warn/wait/defer/shield/health/suspend/restore outcome now use typed identifiers,
actions, policy issues, and reason provenance. Missing accounts and malformed
blocking time, holiday, or communication policy evidence fail with stable domain
errors. The sweep, dry-run, readiness proof, deployment acceptance, and funding
audit continue to consume the same read-only owner; execution still belongs to
the established timer, lifecycle, and access writers.

The current completed access slice is `auth.system_user_assignments`. It is the
only application writer for system-user role and direct-permission grants. Local
administration and ERP HR now converge only their own grant source, profile edits
cannot erase source-managed access, and active-state changes use the staff owner.
Admin-role removal and deactivation serialize on the canonical admin role row and
fail closed if they would leave no active administrator. Assignment state, audit
evidence, the versioned event, and post-commit cache invalidation share one owner
command boundary.

The completed RBAC catalog slice is `auth.rbac_catalog`. It is the single
application and seed writer for roles, permissions, and role-permission policy.
API and admin adapters now submit typed commands; role form updates and policy
replacement commit atomically with audit and versioned event evidence. Catalog
identities are normalized and protected by database functional uniqueness,
assigned identities fail closed on rename or deactivation, and protected
permissions cannot be granted outside the canonical admin role. The remaining
subscriber authorization boundary is now `auth.subscriber_assignments`, the
single application and seed writer for subscriber role and direct-permission
grants. It enforces active catalog references, explicit global or
region/reseller scope, `rbac:assign` command evidence, atomic audit/event
evidence, and post-commit cache invalidation. The legacy `auth.rbac` module and
reseller/seed parallel writers are retired. The catalog cutover reduces the
undeclared writer-like module baseline from its branch-start value of 270 to
269; the assignment cutover removes one service-layer HTTP-exception module and
reduces that separate baseline from 224 to 223.
The referral account-conversion cutover removes another service-layer
HTTP-exception module and reduces that baseline from 223 to 222.
The referral-program cutover removes the next service-layer HTTP-exception
module and reduces that baseline from 222 to 221. It also retires the owner's
direct environment read and delivery-transport bypass, reducing the
decision-input inventory from 351 to 350 occurrences across 83 files and
removing `referrals.program` from the communication-ledger bypass backlog.
The account-adjustment cutover removes another service-layer HTTP-exception
module and reduces that baseline from 221 to 220; its manifest migration
reduces the indexed legacy-service baseline from 222 to 221.

After reconciling `origin/main` through PR #1487, five additional upstream
ownership slices reduce the current undeclared writer-like baseline from 269 to
264. Those upstream declarations must satisfy this branch's typed manifest and
transport/transaction boundaries before the integration state is green.

The integration-platform and vendor field-review cutovers from PRs #1495 and
#1497 retire six more undeclared writer-like integration modules, reducing the
current baseline from 261 to 255. Their four new integration authorities and two
new staff-review confirmation coordinators are fully contracted rather than
being added to the legacy-service baseline; removal of the retired hook and
webhook-delivery owners reduces that baseline from 213 to 211.

These are burn-down measures, not approved exceptions. Reproducible inventory
and shrink-only enforcement are part of the foundation workstream.

Reproduce the current inventory with:

```bash
python -m scripts.architecture.sot_debt
```

## Objective

Make the checked-in source-of-truth doctrine an executable coding contract.
Every important fact, interpretation, decision, transition, projection, repair,
and external consequence must have one named owner whose boundary is enforced
across web, API, task, event-handler, command, and integration entry points.

This is a controlled ownership refactor. It is not complete when code has only
been moved, renamed, registered, or prevented from creating new debt. Existing
drift and parallel paths must also be repairable and retired.

## Delivery model

The refactor may be developed as small, reviewable domain slices. Each slice
must finish one coherent ownership boundary and pass its focused checks before
being incorporated into this branch.

This branch is the integration boundary for the major change. It must not be
merged to `main`, published as a release, or marked complete until every gate
below is satisfied. Intermediate commits, pushes, pull requests, integration
merges, release labeling, and publication require Michael's explicit request.

## Required workstreams

1. **Repository coding contract**
   - Add repository-local contributor and agent guidance.
   - Establish one transaction, error, typing, command/query, event, migration,
     deletion, and projection/reconciliation standard.
   - Replace checked-in examples that teach conflicting or legacy patterns.
2. **Executable ownership registry**
   - Classify record, resolver, policy, writer, reconciler, projection, and
     transport roles explicitly.
   - Record authoritative inputs, transaction boundaries, event contracts,
     freshness and repair obligations, cutover state, tests, and stewardship.
   - Generate or mechanically validate the relationship map from the registry.
3. **Writer and adapter boundaries**
   - Cover services, web, API, tasks, event handlers, scripts, caches, external
     projections, and raw database paths.
   - Remove direct adapter transactions and parallel decision paths.
   - Retire the undeclared-writer and decision-input debt baselines rather than
     treating them as permanent allowlists.
4. **Domain migration slices**
   - Migrate risk-first, beginning with network, access, provisioning, billing,
     and customer-impact paths.
   - Include locking, idempotency, constraints, drift detection, repair,
     fallback retirement, and architecture tests in each slice.
5. **Contract evolution and traceability**
   - Version event, webhook, and public API contracts.
   - Preserve actor, tenant/scope, command/idempotency, correlation, causation,
     aggregate version, reason, and input provenance where applicable.
6. **Quality and release enforcement**
   - Make architecture, security, secret scanning, import boundaries,
     PostgreSQL integration, migrations, and a ratcheted coverage floor
     blocking release-image dependencies.
   - Keep all exceptions narrow, justified, tested, and shrink-only.

## Definition of complete

The major refactor is complete only when all of the following are true:

- Every persistence writer and business decision path is declared under one
  unambiguous owner; duplicate concern and canonical-writer claims are zero.
- Web, API, Celery, event-handler, CLI, and integration adapters delegate to the
  same command/query owners and do not own business transactions.
- The undeclared-writer and decision-input bypass baselines are empty. Any
  infrastructure exception is represented as an explicit owner, not legacy
  debt.
- Public owner commands have defined transaction, locking, idempotency, error,
  audit, event, and retry semantics.
- Every derived field, cache, summary, mirror, and external projection in scope
  has authoritative inputs, temporal/freshness semantics, a drift signal, and
  an idempotent repair path.
- Migration state is explicit for every moved boundary: old owner, new owner,
  verification or shadow phase where required, cutover evidence, and retired
  fallback.
- Domain services expose domain results/errors; HTTP and task semantics are
  mapped only in adapters. New and migrated owner modules pass strict typing.
- Event and external contracts have an explicit compatibility and versioning
  policy, with replay and consumer tests where applicable.
- Deletion, retention, legal-hold, tombstone, and propagation ownership is
  documented and enforced for affected domains.
- The executable registry, generated/validated relationship map, architecture
  tests, developer guidance, and implementation agree.
- Repository-prescribed formatting, linting, typing, security, unit,
  architecture, PostgreSQL integration, migration, and relevant browser/mobile
  checks are green on the final integrated head.
- The final pull request is labeled `version:major`, documents migration and
  rollback/forward-fix implications, and all required CI checks are actually
  green before Michael authorizes merge.

## Merge prohibition

Until the Definition of complete is satisfied, the branch remains
`active, not merge-ready`. Partial progress must be reported as incomplete and
must not be represented as a finished SOT migration.
