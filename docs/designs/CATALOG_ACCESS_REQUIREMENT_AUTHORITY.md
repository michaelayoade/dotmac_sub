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
- `OfferVersions.create` (`app/services/catalog/offers.py`) is a THIN
  ADAPTER: it builds an `AdmitOfferVersionCommand` and calls
  `offer_access_requirement.admit_offer_version`, which is the actual and
  only writer — offer lookup, catalog-default resolution, access-requirement
  admission validation, the `OfferVersion` INSERT, and the
  billing-governance audit participant all run inside ONE
  `execute_owner_command` boundary owned by the new module. The adapter does
  not construct the row itself. `unclassified` remains an accepted explicit
  value in Release 1 — the server-side default exists only for historical
  migration, never as an application-level fallback for a new row.
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
- Admission authorization is decided ENTIRELY at the route layer
  (`app/api/catalog.py`'s `require_any_permission(catalog:billing_write,
  catalog:offer_version:admission)` dependency on `POST`/`PATCH
  /offer-versions`) — matching this repo's own existing pattern in
  `app/services/billing/subledger_opening.py`. That dependency is NOT the
  whole story: both routes also sit under this router's own pre-existing
  `catalog:write` gate (`require_method_permission("catalog:read",
  "catalog:write")`, applied to every mutating route in the file). The
  ACTUAL effective requirement is therefore the compound `catalog:write AND
  (catalog:billing_write OR catalog:offer_version:admission)` — the two
  admission permissions are an OR-alternative to EACH OTHER, never a pure
  standalone alternative to `catalog:write` itself. This is this router's
  established, pre-existing pattern (every other `catalog:billing_write`
  route in this file — offers, offer-prices, add-on-prices — already
  requires `catalog:write` too), not a regression introduced by admission.
  `AdmitOfferVersionCommand` makes NO authorization decision of its own: it
  takes a REQUIRED, typed `AdmissionPrincipal` (`StaffPrincipal` |
  `ApiKeyPrincipal` | `SystemAdmission`), validated at construction
  (`__post_init__`) to actually be one of those three, and recorded purely
  for audit/attribution, never re-checked against RBAC. `catalog:offer_
  version:admission` (`alembic/versions/609_offer_version_admission_
  permission.py`) is a genuine, narrower, OPT-IN alternative to `catalog:
  billing_write` — a caller holding either (in addition to `catalog:write`)
  satisfies the route — so the migration seeds only the permission row
  (mirroring 608's pattern exactly) and copies no grants: there is no
  existing-caller regression to prevent, because nobody's existing
  `catalog:billing_write` access is narrowed or removed.
  `app/api/catalog.py`'s `_admission_principal` narrows the attributable
  principal on BOTH routes (POST and PATCH, identically) to an authenticated
  `system_user`/`api_key` — a deliberate, documented tightening, not a
  silent regression: no other principal type could ever have reached either
  route, because both already require `catalog:write`, which is admin-only
  and never UI-assignable to a non-admin role
  (`scripts/seed/seed_rbac.py`'s `ADMIN_ONLY_PERMISSION_KEYS`), and the
  `admin` role bypasses permission checks entirely rather than being
  attributed as some other principal type.
  `SystemAdmission` (an admission with no authenticated end-user context at
  all) has NO production construction site at all: `OfferVersions.create`'s
  `actor_id`/`actor_type` resolution FAILS CLOSED (raises a typed
  `OfferAccessRequirementError`) for any combination it doesn't recognize as
  `system_user`/`api_key`, instead of silently defaulting to
  `SystemAdmission`; an internal/test caller that genuinely has no
  authenticated actor must construct `SystemAdmission(reason=...)` and pass
  it explicitly via the distinct `principal=` argument. This is proven by an
  AST-based (real `ast.Call` node inspection, not a substring search),
  test-enforced allowlist guard with its own planted-leak and near-miss
  sensitivity proofs (`tests/architecture/
  test_offer_access_requirement_boundary.py`) — a build-time/reviewed-call-
  site guarantee, not an unforgeable runtime credential.
- `(offer_id, version_number)` is enforced as a real DB-level unique
  constraint (`uq_offer_versions_offer_id_version_number`,
  `alembic/versions/610_offer_versions_unique_version_number.py`), not only
  by the advisory lock and pre-insert check. The pair is also immutable
  after admission: `OfferVersionUpdate` has neither field, and
  `OfferVersions.update` asserts this defense-in-depth, the same pattern as
  `access_requirement`'s own exclusion below.
- Admission accepts an optional `Idempotency-Key` header
  (`POST /offer-versions`), recorded in the shared `idempotency_keys` ledger
  (scope `offer_version_admission`): a retried admission that reuses the
  same key and the same request returns the original row instead of a
  `duplicate_version_number` conflict. A request with no header is not
  idempotent.

## Reviewed classification command

`offer_access_requirement.classify_offer_version_access_requirement` is the
only way an `unclassified` row becomes `network_access` or
`no_network_access`. It runs inside `execute_owner_command`
(`app/services/owner_commands.py`) — one root transaction, verified against
this module's `ServiceContract`.

- **Preview** binds the offer version id, its current classification, the
  proposed classification, the row's `updated_at`, and the review reference
  into a SHA-256 fingerprint. If this exact transition (offer version ->
  proposed target) was already recorded, the preview reflects that recorded
  transition and its STORED fingerprint (`already_applied=True`) instead of
  raising — this is what lets a genuine retry (same idempotency key, same
  inputs) reach the command's replay branch rather than being refused before
  it ever tries. A version already classified to a DIFFERENT target, or
  classified with no recorded row at all (e.g. admitted directly with a real
  value), still previews as a refusal.
- **Apply** requires the exact fingerprint, the authenticated principal's
  `SystemUser` id, a reason, a review reference, an idempotency key, and
  explicit confirmation (`--confirm` on the CLI).
- **Identity is never a free-text argument.** The CLI has no separate
  `--actor` field. The string recorded as `classified_by`, the audit actor,
  the event actor, and `authenticated_principal` is always
  `principal_label(authorized_system_user_id)` — derived server-side from
  the id RBAC actually verified, never from anything the caller can type.
- **Permission is re-verified fresh, inside the command's own transaction**
  (`_verify_classify_permission`), not trusted from an earlier, separately
  computed boolean. A grant revoked between an operator's preview and their
  apply is caught here. This NARROWS but does not fully ELIMINATE the
  look-then-act window: no RBAC row (`system_users`, `roles`,
  `role_permissions`, `permissions`) is locked, so a revocation committed in
  the instant between this re-check and the write's commit is not observed.
  This CLI's trust model is host/container shell access, not RBAC alone (see
  "RBAC" below) — closing that residual window would require row-locking
  the entire RBAC surface across five-plus tables, judged disproportionate
  to a trust-the-operator CLI boundary with no pre-authorizing route.
- **Refusals:** a stale preview (fingerprint mismatch), a missing offer
  version, a proposed target of `unclassified`, an idempotency key reused
  with different command inputs (`idempotency_conflict` — a typed error, not
  a raw unique-constraint violation), and any real-to-real or
  real-to-unclassified change — all fail closed.
- **Idempotent replay:** at most one row of
  `offer_access_requirement_classifications` ever exists per offer version,
  and `idempotency_key` is globally unique. An exact replay requires the SAME
  idempotency key AND matching offer version, target, preview fingerprint,
  reason, and authenticated principal — matching only some of those is a
  typed `idempotency_conflict`, not a replay.
- **Evidence:** one audit event (`stage_audit_event`, action
  `offer_access_requirement_classified`) and one versioned domain event
  (`EventType.catalog_offer_access_requirement_classified`) are staged in the
  same transaction, carrying bounded identifiers, the old/new value, the
  command/correlation ids, the review reference, and the authenticated
  principal.

## RBAC: a claimed identity checked against real grants, not a free-text label

`catalog:offer_access_requirement:classify` (migration
`608_offer_access_requirement_classify_permission`) is:

- **Not `catalog:billing_write`.** It is a new, narrow permission.
- **Not seeded into any role.** The migration inserts the permission row only
  — no `role_permissions` row is added for any role.
- Checked via the real RBAC mechanism
  (`app.services.auth_dependencies.has_permission`) against an actual
  `SystemUser` row's roles — the CLI
  (`scripts/catalog/classify_offer_access_requirement.py`) requires
  `--actor-system-user-id` to name an active staff principal, and the
  permission check itself runs inside
  `offer_access_requirement._classify`, fresh, at apply time.
  `--actor-system-user-id` is the operator's CLAIMED identity, not a
  free-text display name: it is the ONLY identity input, and it is resolved
  against real RBAC grants, re-verified fresh inside the command's own
  transaction, before the owner ever treats the action as authorized. **This
  CLI does not itself verify who is really typing the command** — host or
  container shell access to run it at all is this script's actual
  authentication boundary, the same trust model documented in
  `scripts/billing/correct_customer_subledger_opening.py` and its siblings.
  What IS guaranteed: the string recorded as `classified_by`/audit
  actor/event actor is always derived from the RBAC-verified id
  (`principal_label`), never from an unverified claim, and a claimed id that
  does not hold the permission (or an admin/`*` wildcard grant) is refused.
  This is not a check performed once ahead of time and then trusted — see
  "Permission is re-verified fresh" above.

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

## Migration safety

- The classification table's CHECK constraints enforce the one legal
  transition shape in the DATABASE, not only in service code:
  `previous_access_requirement` is always `unclassified`, and
  `new_access_requirement` is always one of the two real values.
- Downgrade LOCKS both tables (`ACCESS EXCLUSIVE`, inside the migration's own
  transaction) before counting rows, so a concurrent write cannot slip
  between the check and the destructive DDL.
- `SET LOCAL` (not a plain `SET`) scopes the migration's own lock/statement
  timeout to its own transaction, so it can never discard the
  operator-configured global override (`alembic/env.py`) for any later
  migration or statement.

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
