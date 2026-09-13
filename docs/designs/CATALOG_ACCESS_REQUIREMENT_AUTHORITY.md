# Catalog offer-version access-requirement authority

**Status:** Release 1 (2026-09-13). Release 2 is planned, later work — not
built by this change.

## Owner

`service_intent.offer_access_requirement` (`app/services/catalog/offer_access_requirement.py`)
owns exactly:

- access-classified offer-version admission
- the immutable access requirement for an exact offer version
- reviewed classification of legacy/unclassified versions

It is a new, complete, fully-contracted `ServiceContract`
(`app/services/sot_registry/domains/service_intent_control_plane.py`), not an
expansion of `service_intent.catalog_policy`.

## Why a new owner, not an expansion of `service_intent.catalog_policy`

`service_intent.catalog_policy` (`app/services/catalog/policies.py`) owns
catalog policy lookup and offer policy interpretation — policy-set and
dunning-step CRUD. It has nothing to do with whether an offer version
requires last-mile network access. Folding this concern into that owner would
either fake compliance (declaring a concern that module doesn't actually
enforce) or force an unrelated scope expansion onto an owner whose contract,
tests, and callers already exist for a different purpose.

**`service_intent.catalog_policy` and `app/services/catalog/policies.py` are
deliberately left completely untouched by this change.** This is a permanent
design decision, not an oversight: the two concerns will keep evolving
independently, and conflating them would make either change unreviewable in
isolation.

## What changed (Release 1)

- New column `offer_versions.access_requirement`: a 3-value enum
  (`network_access` | `no_network_access` | `unclassified`), non-null, with a
  temporary server default of `unclassified` used ONLY to initialize
  historical rows in the migration's own DDL. There is no heuristic or
  automatic backfill — every existing row simply becomes `unclassified`.
- `OfferVersions.create` (`app/services/catalog/offers.py`) now requires the
  field explicitly (`OfferVersionCreate.access_requirement`, no default) and
  delegates admission validation to
  `offer_access_requirement.validate_admission_access_requirement`.
  `unclassified` remains an accepted explicit value in Release 1 — the
  server-side default exists only for historical migration, never as an
  application-level fallback for a new row.
- The field is immutable outside the reviewed classification command:
  `OfferVersionUpdate` has no `access_requirement` field, and
  `OfferVersions.update` calls
  `offer_access_requirement.assert_access_requirement_immutable` as a
  defense-in-depth guard against a future edit reintroducing it there.
- A read-only, deterministic worklist
  (`offer_access_requirement.list_unclassified_offer_versions`, surfaced by
  `scripts/catalog/classify_offer_access_requirement.py` with no arguments)
  reports every remaining `unclassified` row, paginated, instead of raising
  one `AdminAlert` per row.
- `unclassified` is never read as, defaulted to, or treated like PPPoE or any
  connection-type fallback anywhere in the codebase — see the architecture
  guard below.

## Reviewed classification command

`offer_access_requirement.classify_offer_version_access_requirement` is the
only way an `unclassified` row becomes `network_access` or
`no_network_access`. It runs inside `execute_owner_command`
(`app/services/owner_commands.py`) — one root transaction, verified against
this module's `ServiceContract`.

- **Preview** binds the offer version id, its current classification, the
  proposed classification, the row's `updated_at`, and the review reference
  into a SHA-256 fingerprint.
- **Apply** requires the exact fingerprint, an authenticated principal, a
  reason, a review reference, an idempotency key, and explicit confirmation
  (`--confirm` on the CLI). It is gated by the
  `catalog:offer_access_requirement:classify` permission.
- **Refusals:** a stale preview (fingerprint mismatch), a missing offer
  version, a proposed target of `unclassified`, and any real-to-real or
  real-to-unclassified change — all fail closed.
- **Idempotent replay:** at most one row of
  `offer_access_requirement_classifications` ever exists per offer version
  (a database uniqueness invariant). An exact idempotency-key and proposed-
  target replay reads that row and returns a typed replay outcome instead of
  re-transitioning the offer version a second time.
- **Evidence:** one audit event (`stage_audit_event`, action
  `offer_access_requirement_classified`) and one versioned domain event
  (`EventType.catalog_offer_access_requirement_classified`) are staged in the
  same transaction, carrying bounded identifiers, the old/new value, the
  command/correlation ids, the review reference, and the authenticated
  principal.

## RBAC: real authentication, not host trust

`catalog:offer_access_requirement:classify` (migration
`608_offer_access_requirement_classify_permission`) is:

- **Not `catalog:billing_write`.** It is a new, narrow permission.
- **Not seeded into any role.** The migration inserts the permission row only
  — no `role_permissions` row is added for any role.
- Checked via the real RBAC mechanism
  (`app.services.auth_dependencies.has_permission`) against an actual
  `SystemUser` row's roles — the CLI
  (`scripts/catalog/classify_offer_access_requirement.py`) requires
  `--actor-system-user-id` to name an active staff principal and reads that
  principal's real roles (`system_user_role_names`), the same pattern used by
  `scripts/billing/correct_customer_subledger_opening.py`. It is not a bare
  host-access-plus-actor-string check.

**Wildcard note (by design, not a gap):** this RBAC system already treats the
`admin` role and the `*`/domain wildcard grants as satisfying every
permission automatically. "Not seeded by default" means no explicit grant row
is added for this permission — a principal with an existing wildcard/admin
grant continues to pass, exactly as it does for every other permission in
this system. This is existing, unrelated RBAC behavior and is out of scope
here.

## Expand/contract plan

| Release | Admission | DB default |
| --- | --- | --- |
| **1 (this change)** | `access_requirement` required and explicit; `unclassified` accepted | `unclassified`, historical-row initialization only |
| **2 (later, not built here)** | `unclassified` rejected at admission | dropped |

Release 2 is out of scope for this change and must not be built as part of
it — see the brief that authorized this work.

## Architecture guard

`tests/architecture/test_offer_access_requirement_boundary.py` asserts that
`AccessRequirement` (the model enum) is referenced only inside:

- `app/services/catalog/offer_access_requirement.py` (the owner)
- `app/models/catalog.py` (the model/column)
- `app/schemas/catalog.py` (the schema)
- `alembic/versions/607_offer_access_requirement.py` (the migration)
- `scripts/catalog/classify_offer_access_requirement.py` (the CLI/worklist)
- `app/api/catalog.py` (thin error mapping)
- test files

It never leaks into connection-type, PPPoE, RADIUS, enforcement, or
`missing_login` code.
