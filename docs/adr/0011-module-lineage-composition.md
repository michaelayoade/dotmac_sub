# ADR 0011: Module lineages compose beside Sub's own, bound by Sub revisions

Status: accepted

Date: 2026-08-20

Decision owner: Michael

Affected systems and domains: `alembic/env.py`, `alembic.ini`, `scripts/deploy.sh`
migration step, `docs/PLATFORM_ADOPTION_LEDGER.md`, the network-module suite
adoption gate (starter ADR-0038), and every future installable `dotmac-*`
module Sub adopts.

## Context

Sub is the first cutover target for nine installable network modules (starter
ADR-0038: `dotmac-ipam`, `dotmac-network-inventory`,
`dotmac-network-observability`, `dotmac-network-topology`,
`dotmac-network-assurance`, `dotmac-network-control`, `dotmac-fiber-plant`,
`dotmac-network-access`, `dotmac-pon-access`). Each is a stateful tenant-plane
module owning one immutable `mod_*` schema and one migration lineage. Adopting
them means Sub composes an external Alembic lineage for the first time.

`docs/PLATFORM_ADOPTION_LEDGER.md` names "the S7 ADR" as the gate on eighteen
lines but never rules on it, and what it does say is scoped to the KERNEL's
public lineage, not to module lineages:

> Composing kernel revisions into Sub's `version_locations` would put two
> independent heads in one version table — forbidden before the S7 ADR.

The starter's own publication baseline names Sub's state as the blocker for a
different module programme in the same words, so this is not a network-only
question — `docs/inventories/declared-publication-baseline.json`, row
`dotmac-campaigns`:

> current Sub authority evidence still blocks adoption: Sub pins kernel a50,
> does not compose its lineage, and records consent, idempotency and outbox as
> unresolved S7+ owner collisions.

The kernel pin moved to a81 on 2026-08-20 (see the ledger's pin history), which
retires the first clause. This ADR addresses the second. It does not address
the owner collisions, which are per-domain cutovers and stay out of scope.

### Composing a module lineage is not composing the kernel lineage

Four differences, each verified against the a81 kernel and the built packages
rather than assumed:

1. **A module lineage is an independent root.** `ip_0001_ipam` has
   `down_revision = None` and `branch_labels = ("ipam",)`. It does not attach to
   Sub's chain, cannot renumber it, and cannot be renumbered by it.
2. **The revision-ID collision the ledger fears does not exist here.** Sub's
   prefix guard (`tests/architecture/test_migration_prefix_collisions.py`)
   globs `alembic/versions/*.py` and matches `^([0-9]+)_`. Module revision IDs
   are `ip_0001_ipam`, `ni_0001_...`, `fp_0001_...` — never numeric-prefixed,
   and never in that directory. The guard is not blind to a risk; there is no
   risk for it to see. The ledger's concern was the kernel's `0001_`–`0026_`
   files, which this ADR does not compose.
3. **Multiple heads are already the deployed reality.** ADR-0008 established
   this on 2026-08-07; re-verified at current positions rather than carried
   over, since its line numbers have since drifted —
   `scripts/deploy.sh:325` and `:770`, `Makefile:166` and `:239`, and
   `docs/runbooks/PRODUCTION_DEPLOYMENT.md:48` all run `alembic upgrade heads`,
   plural. Linearity is a test-imposed constraint on Sub's own chain, not a
   deployment one.
4. **Sub does not have to run kernel `0001` to satisfy what modules need.** The
   modules declare database EFFECTS, not revisions —
   `dotmac_ipam.manifest` has
   `requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name)` —
   and the kernel documents a product supplying those from its own lineage as
   the intended case. `dotmac_kernel.prerequisites`, on
   `TENANT_SCOPE_CATALOG_V1`:

   > Kernel 0001 supplies both; so does ERP's `20260813_tenant_projection`, in
   > ERP's own lineage, without any kernel identity/RBAC/audit table existing
   > at all.

   And the reference assembly's `app/migration_bindings.py`:

   > ERP, which hosts `public.tenants` in its own lineage and structurally
   > cannot run kernel `0001`, writes a different file here and installs the
   > same modules.

   That is Sub's position exactly. Sub already hosts `public.tenants` and
   `public.tenant_domains` through migrations 508/509 under ADR-0009.

### The one real defect

`alembic/env.py:101` `_install_idempotent_schema_ops()` monkey-patches nine
module-level `alembic.op` functions so the post-squash chain tolerates state
the squashed `001` migration already built. It is installed unconditionally in
`run_migrations_online()`, after `context.configure` and before
`context.run_migrations()`, so it applies to EVERY revision in the run —
including a composed module lineage written years after the squash it exists to
paper over.

Every guard is schema-blind. `_table_exists`, `_columns_of`, `_index_exists`
and `_constraint_exists` all call `sa.inspect(op.get_bind())` with no `schema=`
argument, so they inspect the search-path schema. `_safe_create_table` accepts
`(table_name, *args, **kwargs)` and passes `schema=` through to the original
while testing existence without it.

Module DDL is fully schema-qualified — `ip_0001_ipam` calls
`op.create_table("addresses", ..., schema="mod_ipam")`. Six module table names
collide with a Sub `public` table of the same bare name:

| Bare name | Module owner | Sub owner |
| --- | --- | --- |
| `addresses` | `dotmac-ipam` | `app/models/subscriber.py` |
| `alerts` | `dotmac-network-observability` | `app/models/network_monitoring.py` |
| `pon_ports` | `dotmac-pon-access` | `app/models/network.py` |
| `ports` | `dotmac-network-inventory` | `app/models/network.py` |
| `sessions` | `dotmac-network-access` | `app/models/auth.py` |
| `vlans` | `dotmac-network-inventory` | `app/models/network.py` |

None is an authority collision — `mod_ipam.addresses` is an IP address and
Sub's `addresses` is a street address; `mod_netaccess.sessions` is a RADIUS
session and Sub's `sessions` is an auth session. The danger is purely
mechanical, which is what makes it dangerous: under today's `env.py`,
`op.create_table("addresses", schema="mod_ipam")` finds Sub's public
`addresses`, returns `None`, and the revision is stamped as applied with the
table absent. The lineage then believes it is up to date and every query
against `mod_ipam.addresses` fails at runtime with `UndefinedTable`.

That is a silent-corruption path, not a migration failure, and it is reachable
today by composing a single module.

## Decision

Sub composes module lineages BESIDE its own chain and supplies their
prerequisites from its own revisions. Sub does not compose the kernel's public
lineage, and this ADR grants no permission to.

1. **Prerequisite bindings are Sub's, and live in one named file.** Sub adds
   `app/migration_bindings.py`, the same decision the reference assembly makes
   in the same filename, binding `tenant_scope_catalog.v1` and
   `module_database_roles.v1` to Sub revisions. Binding is not belief:
   `resolve_depends_on` turns each binding into Alembic's real physical edge at
   script load, and the static composition gate checks that the provider
   revision is composed and declares the effect. Then
   `require_prerequisites` independently proves the effect's live catalog
   observables before the requiring migration emits DDL. `alembic_version`
   records current heads, not applied history, so a provider ancestor is not
   required to remain there as a row.

2. **Two additive Sub migrations close the prerequisite gap.** Measured, not
   assumed — Sub satisfies neither spec in full today:
   - `tenant_scope_catalog.v1` wants `public.tenants` with the kernel column,
     key and index contract AND `public.app_current_tenant_id()` returning the
     `app.current_tenant` GUC as uuid. Sub has the table (508/509) and already
     sets that exact GUC name (`app/services/operator_tenant.py:85`), but
     defines no such function anywhere in `alembic/` or `app/`.
   - `module_database_roles.v1` wants `app_admin`, `app_user` and `platform_api`
     to exist and be grantable. No Sub migration creates any of the three.

3. **The idempotent schema-op wrappers become schema-aware and Sub-scoped.**
   Every guard takes the `schema=` kwarg its wrapped op already receives and
   passes it to the inspector. This is a correctness fix Sub needs whether or
   not it ever adopts a module — the wrappers silently no-op on any qualified
   DDL today — and it is the precondition for composing anything.

4. **The revision-0001 ratchet does not move.**
   `tests/integration/test_kernel_lineage_rehearsal.py::EXPECTED_FIRST_FAILURE`
   stays at `0001_initial_tenant_schema`. Nothing here composes the kernel
   lineage, so nothing here earns a ratchet movement, and the nine competing
   model declarations and lineage-head overlaps keep their existing
   dispositions.

### Rejected alternatives

**Run module lineages under a second Alembic config.** A separate
`alembic_modules.ini` with its own `env.py` and version table would isolate
modules from Sub's squash regime completely, and it is genuinely tempting given
what `_install_idempotent_schema_ops` does. Rejected because it makes the
version table plural, so "what schema is deployed" stops having one answer;
it doubles the deploy migration step and the lock-timeout policy
(`_set_migration_lock_timeout`, `ALEMBIC_LOCK_TIMEOUT`); and it leaves the
schema-blind wrappers unfixed for Sub's own future qualified DDL. It trades a
one-file correctness fix for a permanent second migration authority — the shape
this repository's SOT standard exists to prevent.

**Retire the wrappers entirely by fixing the post-squash chain.** Correct
long-term and explicitly contemplated by the `_install_idempotent_schema_ops`
docstring ("Post-squash migrations must call the top-level `op` schema methods
unless they implement equivalent live-schema guards themselves"). Rejected as a
prerequisite because it is a several-hundred-revision audit that blocks the
suite cutover behind unrelated work. Making the guards schema-aware is
compatible with retiring them later.

## Invariants

- Sub's own migration chain stays single-headed at promotion; module lineages
  are separate heads and are never parented onto it.
- No module revision is named by a Sub revision, and no Sub revision is named
  by a module revision. The only coupling is effect→revision in
  `app/migration_bindings.py`.
- A schema-qualified `op.*` call is guarded against the schema it names, or it
  is not guarded at all. A guard that inspects a different schema than the op
  targets is a defect, not a conservative default.
- `public` remains Sub's; every `mod_*` schema remains its module's. No Sub
  model, migration or service writes a `mod_*` table, and no module writes a
  `public` table.
- Composing a module does not activate FORCE RLS on Sub tables, move the
  revision-0001 ratchet, or admit any kernel module to `app/` beyond the
  ADR-0009 allowlist.
- Every prerequisite Sub binds is proven against the live catalog at upgrade
  time, never asserted in a document alone.

## Consequences

**Operational.** The deploy migration step is unchanged in shape — it already
runs `alembic upgrade heads`. Each composed module adds one head. Lock-timeout
policy applies to module DDL identically, since it is set on the connection
before `run_migrations`.

**Data.** The schema-aware fix changes behaviour for exactly one class of
statement: a qualified op whose bare name exists in `public`. Today those
silently no-op; afterwards they execute. No Sub revision issues qualified DDL
today, so the fix is a no-op against Sub's existing chain — which is also why
it can land ahead of any module work, and should.

**Compatibility.** Sub keeps hosting `tenants`/`tenant_domains` through
ADR-0009 rather than through kernel `0001`, so the ERP precedent is the one Sub
follows and the kernel-lineage collision inventory is untouched.

**Security.** Creating `app_admin`, `app_user` and `platform_api` introduces
cluster roles Sub does not have today. Passwords are never set in migrations
(the kernel spec is explicit). Grants land in the module's own revision; Sub's
migration only ensures the roles exist and are grantable.

**Team.** `app/migration_bindings.py` becomes a reviewed file: a diff there is
an authority statement about which Sub revision supplies a shared effect, and
should be read as one.

**Rejected-alternative cost, recorded honestly.** Making the wrappers
schema-aware leaves the squash-idempotency regime in place. Sub keeps a
monkey-patched `alembic.op` for the foreseeable future, and every future
contributor writing qualified DDL must know that. That is a real cost of not
taking the second-config option.

## Migration and cutover

- **Old owner and paths:** no owner. Module-lineage composition is unruled;
  `alembic/env.py:101` guards silently and schema-blind.
- **New owner and paths:** `app/migration_bindings.py` owns effect→revision;
  `alembic/env.py` owns schema-aware guarding; `alembic.ini` `version_locations`
  owns which lineages are composed.
- **Backfill/repair:** none. No existing row or applied revision changes.
- **Shadow or verification phase:** compose ONE module (`dotmac-ipam`, suite
  order 1) against a production-shaped scratch database, with `mod_ipam.addresses`
  as the named canary — it is the collision that would silently no-op today.
  Prove the table exists after upgrade, and prove the pre-fix `env.py` fails the
  same check.
- **Cutover gate and evidence:** the two prerequisite migrations are applied,
  `require_prerequisites` passes against the live catalog for both effects, the
  single-module rehearsal is green, and Sub's own chain is still single-headed.
- **Fallback retirement:** none in this ADR. Retiring Sub's local network
  writers is each module's own retirement gate in
  `docs/inventories/network-module-dispositions.toml`, sealed at the suite
  cutover, not here.
- **Schema contract step:** two additive Sub migrations —
  `public.app_current_tenant_id()`, and the three database roles. Both are
  forward-only and neither touches an existing table.

## Verification

- **Architecture:** a test asserts no Sub revision names a module revision and
  no module revision names a Sub revision; a test asserts every
  `ModuleManifest.requires` name in the composed set has a binding in
  `app/migration_bindings.py`.
- **Behaviour:** a canary creates a table in a `mod_*` schema whose bare name
  exists in `public` and asserts the table exists afterwards. This test fails
  against today's `env.py` — that is the point, and it should be written before
  the fix.
- **Migration:** `alembic upgrade heads` from the currently deployed revision
  against a production-shaped database, with one module composed, leaves Sub's
  chain single-headed and the module head applied.
- **Reconciliation:** the static composition gate and resolved Alembic edge
  reject an invalid binding or ordering graph; `require_prerequisites`
  separately refuses a structurally wrong live effect before module DDL.
- **Operational:** the deploy script's existing `upgrade heads` path is
  exercised with a composed module; lock timeout is observed on module DDL.
- **Isolation:** cross-tenant RLS canaries for the composed module's tables run
  against real PostgreSQL, per the existing testing model.

## Rollback or forward-fix

The schema-aware guard fix is reversible with no data consequence — it changes
which statements execute, and against Sub's current chain it changes nothing.

The two prerequisite migrations are additive and independently reversible: the
function can be dropped, the roles revoked and dropped if nothing has been
granted to them.

Composing a module lineage is the point of no easy return, and it is the
module's `downgrade()` that owns it — `ip_0001_ipam` drops its own tables and
schema. Removing a composed module means running its downgrade and removing its
`version_locations` entry; rows in `mod_*` are lost, which is why the suite
cutover carries its own rollback rehearsal (starter ADR-0038 §3) and this ADR
does not claim to cover it.

Nothing here touches applied Sub migrations or production data.

## Review and retirement

- Review date: at the network-suite cutover gate, or 2026-11-20, whichever is
  first.
- Retirement condition: superseded if Sub ever composes the kernel's public
  lineage, which would make the ERP-style binding indirection unnecessary and
  require re-ruling the collision dispositions.
- Supersedes or is superseded by: none. Extends ADR-0009 (operator-tenant
  bridge) and depends on ADR-0008's `upgrade heads` finding.

## Amendment — 2026-08-25: a product revision may supply a runtime prerequisite

The original decision named the two effects required by the first network
modules and explicitly left idempotency ownership out of scope. It did not rule
the narrower question now reached by commercial modules: whether Sub may host
kernel-shaped prerequisite storage without adopting the kernel migration
lineage or transferring Sub's existing runtime authority.

It may. Migration `556_idempotency_ledger_prereq` supplies
`idempotency_ledger.v1` from Sub's own lineage and
`app/migration_bindings.py` names that Sub revision as the provider. Both
planes form one indivisible effect. The tenant table is tenant-scoped and
FORCE-RLS protected; the platform table has no tenant column or RLS, is
reachable by the two platform roles, and is fully revoked from `app_user`.
The provider migration's final statement asks the exact pinned kernel verifier
to accept the live catalog before Alembic records the revision.

This does not amend the runtime ownership decision. Existing
`idempotency_keys`, `task_executions`, `IdempotencyKey`, `TaskExecution` and
`idempotent_task` paths remain authoritative, no existing row moves, and
`dotmac_kernel.idempotency` remains forbidden under `app/`. The legacy
reference set is frozen by a two-directional ratchet so coexistence cannot grow
silently; shrinking it requires the same reviewed slice to lower the baseline.
The new table names are storage consumed by future composed-module runtime
paths, not a second Sub idempotency service.

The import boundary changes only for migration entry points. An Alembic
revision may import the exact name
`dotmac_kernel.migrations.verify.require_prerequisites`; application code and
every other verifier name remain refused. This makes “binding is not belief”
true for a product-owned provider while preserving the kernel runtime boundary.

The cutover gate for any consuming commercial module remains separate: pin its
exact-tagged compatible kernel/module artifacts in the lock, compose the selected
plane, prove fresh and predecessor upgrades plus RLS isolation, then run a
shadow/parity phase before moving a business writer. This amendment by itself
moves no subscription, billing, collection, payment, settlement, route, job,
webhook or reconciliation authority.

## Amendment — 2026-08-25: Sub supplies module-event relay storage product-first

Migration `557_outbox_relay_prereq` supplies `outbox_relay.v1` after the
idempotency provider. Its implementation is ported from ERP commit
`dc10b24af22b1452b9954d4c33ff87a5916a4afe`, the qualifying production-used
source, rather than rebuilt beside it. Sub changes only the typed product seam:
its existing `event_store`, owner-output, notification, network-operation and
field-ERP outboxes remain separate authoritative mechanisms.

The two dispatcher roles are cluster identities, so their creation is an
explicitly elevated bootstrap step owned by
`scripts/bootstrap_outbox_dispatcher_roles.py`, not hidden inside ordinary
Alembic execution. The migration first verifies that both identities are LOGIN,
NOBYPASSRLS and NOSUPERUSER, then creates the two relay planes and their four
hardened SECURITY DEFINER functions. PostgreSQL evidence covers the exact
primary/foreign keys, defaults, claim indexes, positive and negative grants,
schema usage, own-plane-only EXECUTE, real `app_user` tenant isolation and the
pinned kernel verifier's negative observables.

Storage is not delivery. This amendment binds a repairable prerequisite now
consumed by shadow-composed commercial modules but installs no worker, route,
webhook or runtime module import. Existing Sub delivery paths do not enqueue
into or drain the new tables, so no event or money authority moves here.

Revisions 556 and 557 deliberately add four names to the executable
kernel-lineage collision inventory: `idempotency_records`,
`platform_idempotency_records`, `outbox_events`, and
`platform_outbox_events`. They are verified kernel storage providers whose
disposition is STAMP, not competing business authorities. The reviewed current
head inventory therefore grows from ten names to fourteen; the rehearsal
ratchet records that classification explicitly.

## Amendment — 2026-08-25: Subscriptions tenant storage is shadow-composed

Sub now exact-pins tagged `dotmac-subscriptions==0.1.0a3` artifacts and its
required `dotmac-kernel==0.1.0a94`, declares the installed Subscriptions migration
resource in `alembic.ini`, and selects only `ModulePlane.TENANT` in
`app/migration_bindings.py`. `alembic/env.py` installs that selection before
the module's migration executes. The declaration is independent from the
four prerequisite bindings: the bindings record effects this database really
has, while the selection records storage this product intends to install.

The package resource belongs in `alembic.ini`, not in a dynamic mutation in
`env.py`. Alembic creates its `ScriptDirectory` before executing `env.py`, so a
late `config.set_main_option("version_locations", ...)` can report a successful
upgrade while silently omitting an installed module lineage. The architecture
gate now checks pins and package resources in both directions.

This is an additive empty-schema/shadow phase. It introduces no application
import, route, job, webhook, runtime reader, runtime writer, backfill or
dual-write, and it leaves the deployment profile's commercial provider set to
`none`. Sub's local subscription, billing, collection and payment owners remain
authoritative. The tenant-plane selection cannot be read as an authority claim:
Vendor CP remains the first Subscriptions authority cutover on the platform
plane, and Sub remains second behind complete parity and a separately sealed
authority switch. Released a3 supplies the billing-treatment contract needed
by the next parity phase, but this additive schema pin does not satisfy the
backfill, comparison or sealed authority gates.

Disposable-PostgreSQL evidence covers a fresh `upgrade heads`, an upgrade from
Sub revision 556, exact selected/unselected table catalogues, ENABLE + FORCE
RLS on all selected tables, effective two-tenant visibility as `app_user`,
provider-ledger preservation and repeat-upgrade stability. Production is not
touched and this amendment authorizes no deployment.

## Amendment — 2026-08-25: Payments tenant storage joins the shadow composition

`dotmac-payments==0.1.0a1` is exact-pinned and its installed
`pm_0001_payment_intents` root composes beside Sub, Service Orders and
Subscriptions. Payments is atomic tenant-only, so its manifest already fixes
the full persistence plane and no `ModulePlaneSelection` is permitted.

Composing its lineage and empty schema does not select a runtime commercial
provider or transfer payment authority. Sub's legacy payment-intent,
confirmation, proof and provider-consequence writers remain authoritative
until a distinct data-bearing cutover seals parity and retires them. The real
PostgreSQL canary verifies the exact table catalogue and ENABLE + FORCE RLS,
then seeds an operator row before exercising unset, wrong and canonical tenant
contexts as `app_user`; this prevents a wrong-tenant read assertion from
passing vacuously against an empty table. Repeat `upgrade heads` is a no-op.

## Amendment — 2026-08-25: Billing and Collections tenant storage joins shadow

Sub exact-pins `dotmac-billing==0.1.0a1` and
`dotmac-collections==0.1.0a1`, declares both installed migration resources in
`alembic.ini`, and selects `ModulePlane.TENANT` for both dual-plane modules in
`app/migration_bindings.py`. The resulting independent heads are
`bi_0001_billing` and `cl_0001_collections`. Only their tenant catalogues are
installed; their platform tables remain absent. Fresh and
`557_outbox_relay_prereq` predecessor rehearsals prove the exact catalogues,
ENABLE + FORCE RLS, effective two-tenant isolation, provider-row preservation
and repeat-upgrade stability.

This is still an empty-schema/shadow decision, not runtime adoption.
`commercial_provider="none"` remains the selected profile value, application
code imports neither distribution, and no reader, writer, route, job, webhook,
dispatcher, backfill or dual-write is introduced. Sub's existing billing,
payment, settlement, allocation and Collections decision/consequence paths
retain authority.

Composition also preserves the cross-product cutover order. Billing and
Subscriptions are Vendor-first: their Vendor CP platform-plane authority
switches must complete before the matching Sub tenant-plane switches.
Collections is Sub-first: Sub must prove and seal the first tenant-plane
authority switch before Vendor CP may move its platform plane.

Each authority move remains separately gated by a typed complete backfill,
total-classified shadow parity, a named sealed watermark with an explicit
rollback premise, and retirement of the local writers, jobs, fallbacks and
repair paths under sensitivity-proven ratchets. Billing's cutover is one
coupled invoice/settlement/allocation boundary. Collections additionally
requires exact parity of the authoritative Billing observation before a policy
cohort moves. Subscriptions retains its distinct a3 billing-treatment and
recurrence gates. None of those gates is satisfied merely by this amendment.

## Amendment — 2026-08-25: a pure Collections observation precedes authority

Sub may now import only the Collections a1 public
`ReceivableObservationV1` value object in one named read-only adapter. The
adapter compares the incumbent postpaid invoice-candidate predicate with the
value object's pure blocker inside a repeatable-read, read-only snapshot and
emits aggregate, PII-free evidence. It is registered as
`collections.module_shadow_parity`, a resolver in `SHADOWING`, rather than as
an authoritative record, policy or writer.

The command's default non-zero mismatch result is an operational check, not a
cutover gate. Its aggregate output deliberately carries no cohort identity,
source revision, evaluation instant or evidence digest, so it cannot seal the
representative cohort required by this ADR. It also preserves the incumbent's
second raw Python-truthiness reconciliation-hold check, including the legacy
string `"false"` quirk, and reports rather than normalizes that mismatch.

The observer preserves the exact incumbent/module blocker pair, not only their
binary decisions. It can evaluate those same immutable snapshot inputs again at
an explicit later instant and aggregates every parity transition. This exposes
the temporal case hidden by the old `MATCHED_BLOCKED` bucket: a future-due
invoice without due provenance is blocked as `receivable_not_due` /
`due_date_unverified` initially, but the incumbent becomes actionable at the
due instant while the module remains fail-closed. Naive instants and an
observation before evaluation are refused; an equal instant is deterministic.
Serialized output carries neither absolute instant, identifier nor amount—only
the observation horizon in seconds—so this evidence remains operational and
unsealed.

The admission does not include `CollectionCaseService`, a Collections
submodule, Billing or Subscriptions runtime APIs, module writes, DDL, backfill,
routes, jobs, outbox delivery, idempotency or consequences. The deployment
profile remains `commercial_provider="none"`; Sub's incumbent invoice,
dunning and financial-access owners remain in force. Collections remains the
Sub-first cutover, but Billing and Subscriptions retain their Vendor-first
ordering and are not adopted indirectly by this observer.

Collections a1 requires a positive `source_version` on the observation. This
report uses the constant `1` only as an inert contract marker. It is not a
stateful reader version and cannot cross into `CollectionCaseService`; Sub
lacks one durable monotonic revision spanning every fact that can change a
receivable. The authority gate therefore remains zero total-classification
mismatches at the evaluation instant and across an approved temporal horizon on
sealed representative data, exact Billing-input parity, a real monotonic source
version, policy/case/consequence parity and explicit retirement of incumbent
writers and jobs.
