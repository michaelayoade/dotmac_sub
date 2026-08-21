# Cohort-isp-01 source ownership map

Sub's side of Governance cohort `cohort-isp-01`, "Foundation party and
customer". This document is the human-readable half of
`app/migration_source/`; the machine-checked half is
`app/migration_source/surfaces.py`, and
`tests/architecture/test_isp_cohort_source_writers.py` fails if the two
disagree about who writes what.

Read `docs/adr/0012-isp-cohort-source-readiness.md` first for the decision this
implements.

## Governance binding

| | |
|---|---|
| Programme | `pgm-dotmac-isp-replacement` |
| Accepted revision | `68c7a62e2aafd9c236662a5a69d410ea002b4cdb` |
| Record | `programmes/dotmac-isp-replacement.json` in `dotmac_governance` |
| Decision | `docs/adr/0012-dotmac-isp-replacement-programme.md` (Accepted) |
| Source assembly | `asm-dotmac-sub-legacy` — **source-authoritative** |
| Target assembly | `asm-dotmac-isp` — candidate, independent database |
| Track | `track-isp-sub-cutover` |
| Cohort | `cohort-isp-01`, sequence 1, state **blocked** |
| Components | kernel (reuse), UI (reuse), `dotmac-party` (release), `dotmac-brand-profiles` (adopt), `dotmac-customers` (build) |

Sub's readiness work produces inputs for `ctl-isp-006` (source dispositions and
idempotent replay), `ctl-isp-007` (zero-unexplained-drift shadow) and
`ctl-isp-009` (bidirectional writer ratchet to zero). Producing an input is not
verifying a control. Every one of those three is `blocked`, and the two open
decisions that gate the cohort — `dec-isp-002`, the production deployment
owner, and `dec-isp-003`, the enforceable legacy Sub transition rule — are
Michael's and are unresolved.

**Nothing in this repository opens the cohort.** Sub is the sole production
writer of every fact below until a separately authorised sealed switch.

## Tenant scope

Sub is a dedicated single-operator deployment (ADR 0009). The ISP operator
*is* the tenant, and the cohort's tables carry no `tenant_id` column; the
operator tenant is a deterministic identity installed as a transaction-local
GUC on every root transaction.

Tenant scope is therefore mandatory and non-nullable in the export contract
and is resolved from `tenancy.operator_tenant`, never from a caller argument.
A request naming any other tenant is refused rather than answered with an
empty result — an empty page and a refusal look identical to an importer, and
only one of them is safe.

None of these tables lives in a platform/control plane. All twelve are
operator-tenant data.

## The twelve entity types

| Entity type | Table | Model | Declared owner | Expected target component |
|---|---|---|---|---|
| `party` | `parties` | `Party` | `party.registry` | `dotmac-party` |
| `party_role` | `party_roles` | `PartyRole` | `party.registry` | `dotmac-party` |
| `party_relationship` | `party_relationships` | `PartyRelationship` | `party.registry` | `dotmac-party` |
| `party_membership` | `party_memberships` | `PartyMembership` | `party.registry` | `dotmac-party` |
| `party_contact_point` | `party_contact_points` | `PartyContactPoint` | `party.registry` | `dotmac-party` |
| `party_external_reference` | `party_external_references` | `PartyExternalReference` | `party.registry` | `dotmac-party` |
| `customer_account` | `subscribers` | `Subscriber` | `customer.accounts` | `dotmac-customers` |
| `customer_contact` | `subscriber_contacts` | `SubscriberContact` | **none declared** | `dotmac-customers` |
| `customer_address` | `addresses` | `Address` | **none declared** | `dotmac-customers` |
| `organization` | `organizations` | `Organization` | **none declared** | `dotmac-customers` |
| `organization_membership` | `organization_memberships` | `OrganizationMembership` | **none declared** | `dotmac-customers` |
| `brand_profile` | `brand_profiles` | `BrandProfile` | `customer.branding` | `dotmac-brand-profiles` |

The expected target component is an expectation recorded from the Governance
matrix. It is not a plan, an approval, or a claim that any module is released.

## Writer census

Counted mechanically across every entry-point family by
`scripts/architecture/isp_cohort_writers.py` and frozen in
`tests/architecture/isp_cohort1_writer_baseline.txt`.

| Family | Files | Write sites |
|---|---|---|
| `service` | 17 | 66 |
| `web_presenter` | 5 | 13 |
| `cli_script` | 6 | 14 |
| `migration` | 6 | 8 |
| **total** | **34** | **101** |

`api_route`, `web_route`, `task_worker`, `scheduled_job`, `event_handler`,
`websocket`, `importer`, `poller`, `app_module` and `repository_root` produce
**zero** counted writes. That is a real property of this repository rather
than a gap in the scan: the thin-adapter rule already keeps routes and tasks
out of direct persistence, and the census's sensitivity tests prove it would
count a route that started writing.

### Classification

| Classification | Files |
|---|---|
| `AUTHORITATIVE_WRITER` | 6 |
| `DERIVED_PROJECTION` | 2 |
| `LEGACY_PARALLEL_WRITER` | 26 |
| `AUTHORIZED_ADAPTER` | 8 |
| `READ_ONLY_CONSUMER` | 1 |
| `OBSERVATION_COLLECTOR` | 0 |
| `TRANSPORT` | 0 |
| `UNKNOWN` | 0 |

Of the 34 writing surfaces, **26 are production runtime**. The other eight
cannot write the production database again: `scripts/seed/seed_test_fixtures.py`
and `scripts/migration/kernel_lineage_rehearsal_canaries.py` touch only
disposable databases, and the six applied migrations below are applied —
Alembic will not re-run them against a migrated database. All eight stay in the
ratchet, because a *new* one is exactly what the guard should catch; none of
them counts as something a cutover has to displace, because none of them can
be retired.

### Declared owners

| Path | Owner | Writes |
|---|---|---|
| `app/services/party.py` | `party.registry` | 19 |
| `app/services/subscriber.py` | `customer.accounts` | 14 |
| `app/services/web_customer_actions.py` | `customer.profile_commands` | 7 |
| `app/services/brand_profiles.py` | `customer.branding` | 2 |
| `app/services/subscriber_profile_cleanup.py` | `customer.profile_cleanup` | 2 |
| `app/services/crm_customer_name_repair.py` | `customer.name_remediation` | 1 |

`web_customer_actions.py` is named like a presenter and declared as an owner.
It stays in the `web_presenter` family because that is where a reader will
find it, and it is classified as an owner because the registry says so. Family
is location; classification is authority. Conflating them is how a real owner
gets mistaken for a stray adapter — or the reverse.

### Derived projections written onto cohort rows

| Path | Owner | Fact |
|---|---|---|
| `app/services/account_lifecycle.py` | `access.subscription_lifecycle` | Subscriber lifecycle status and its override columns |
| `app/services/mrr_snapshot.py` | **none declared** | `subscribers.mrr_total` |

Both must cross to the destination as *derived*, never as the account's own
state. `mrr_total` is the sharper risk: a money figure, computed elsewhere,
stored on the account row, written by a module the registry does not declare.
The target recomputes it; it does not trust it.

### Legacy parallel writers — the displacement list

Twenty-six files write a cohort fact some other owner is declared to own.
**Eighteen of them can do it again**, and that eighteen is the set `ctl-isp-009`
must ratchet to zero; the remaining eight are listed for completeness and
marked non-production.

| Path | Bypasses | Entity |
|---|---|---|
| `app/services/account_deletion.py` | `customer.accounts` | account `metadata` |
| `app/services/billing_cleanup_remediation.py` | `customer.accounts` | `billing_mode` |
| `app/services/crm_portal.py` | `party.registry` | `crm_subscriber_id` |
| `app/services/crm_ticket_pull.py` | `party.registry` | `crm_subscriber_id` |
| `app/services/customer_location_requests.py` | `customer.accounts` | `Address`, account `metadata` |
| `app/services/customer_portal_contacts.py` | `party.registry` | `subscriber_contacts` |
| `app/services/customer_portal_notifications.py` | `customer.accounts` | account `metadata` |
| `app/services/network_subscriber_bridge.py` | `customer.accounts` | constructs `Subscriber` |
| `app/services/nin_verifications.py` | `customer.accounts` | account `metadata` |
| `app/services/team_inbox_commands.py` | `party.registry` | constructs `PartyRelationship` |
| `app/services/web_admin_resellers.py` | `customer.accounts` | `reseller_id` |
| `app/services/web_customer_details.py` | `customer.accounts` | account `metadata` |
| `app/services/web_system_import_wizard.py` | `customer.accounts` | constructs `Subscriber` |
| `app/services/web_system_restore_tool.py` | `customer.accounts` | `is_active`, account `metadata` |
| `scripts/migration/backfill_crm_subscriber_links.py` | `customer.accounts` | raw DML |
| `scripts/migration/backfill_party_status.py` | `party.registry` | raw DML |
| `scripts/migration/import_crm_phase3.py` | `customer.accounts` | raw DML |
| `scripts/one_off/backfill_crm_subscriber_ids.py` | `customer.accounts` | raw DML |
| `scripts/seed/seed_test_fixtures.py` | — | fixtures only, non-production |
| `scripts/migration/kernel_lineage_rehearsal_canaries.py` | — | rehearsal canaries, non-production |
| `alembic/versions/045_contact_channels_without_name.py` | — | applied migration, non-production |
| `alembic/versions/116_add_billing_accounts.py` | — | applied migration, non-production |
| `alembic/versions/208_map_karu_customers_to_bts.py` | — | applied migration, non-production |
| `alembic/versions/267_brand_profiles.py` | — | applied migration, non-production |
| `alembic/versions/277_lifecycle_communications_sot.py` | — | applied migration, non-production |
| `alembic/versions/383_replaceable_backoffice_boundary.py` | — | applied migration, non-production |

### The `subscribers.metadata` finding

Seven modules write `subscribers.metadata`: account deletion, NIN
verification, portal notification read-state, CRM name repair, location
requests, the customer detail presenter and the system restore tool. No owner
declares its shape, no schema constrains it, and its keys are added by
whichever feature needed somewhere to put something.

This is the single most consequential fact in the cohort for migration
purposes, and it is why the export contract treats the blob as an **inventory
of keys plus a digest**, never as typed facts. A migration that reads
structure into that column is inventing a contract nobody owns, and it would
carry seven features' private conventions into the destination as though they
were data.

## Adapters and callers

Nine adapters reach cohort state and write none of it. They validate,
authorise, delegate to an owning service, and own only the transaction —
which is what `db.commit()` in a route means here.

| Path | Family | Guard |
|---|---|---|
| `app/api/subscribers.py` | `api_route` | `require_permission("customer:read"/"create"/"update"/"delete")` |
| `app/api/me.py` | `api_route` | authenticated customer session |
| `app/api/reseller.py` | `api_route` | reseller-scoped permission |
| `app/web/admin/customers.py` | `web_route` | `require_permission("customer:read"/"write")` |
| `app/web/admin/system.py` | `web_route` | admin permission |
| `app/web/admin/billing_payments.py` | `web_route` | billing permission — reads accounts only |
| `app/web/customer/routes.py` | `web_route` | authenticated customer session |
| `app/web/customer/location.py` | `web_route` | authenticated customer session |
| `app/tasks/nin_tasks.py` | `task_worker` | worker context; writes verification rows outside this cohort |

## Deliberate exclusions

Ten cohort-adjacent tables are outside the export contract, each recorded in
`surfaces.UNMAPPED_ADJACENT_TABLES` with its reason. In summary: `resellers`
and `reseller_users` belong to Governance cohort 7; `customer_identity_index`
is a rebuildable search index; `subscriber_channels` belongs with cohort 6
communications; `subscriber_nin_verifications` is regulatory evidence needing
its own disposition decision; `subscriber_custom_fields` has no declared
schema; and `carried_source_identity_adjudications`,
`party_identity_backfill_receipts` and the two
`subscriber_contact_*_projections` tables describe the migration rather than
constitute source state.

## Two tables with no counted writer

`organizations` and `organization_memberships` have **zero** counted writers.
No construction, no tracked mutation, no set-based DML and no raw statement
names them anywhere under `app/`, `scripts/` or the executable migration
lineage — apart from one applied migration writing back-office reference
columns on `organizations`.

This is a searched result, not UNKNOWN, and it is not proof. The census cannot
see a mutation through a local it could not bind to a cohort model, a generic
`setattr` helper, a SQLAlchemy event hook, or a writer outside the scanned
roots. It reads "no counted writer" for that reason. The disposition of these
rows — historical B2B account records with no live owner — is an open item for
`ctl-isp-006`.

## Unresolved

- `dec-isp-002` — production deployment owner for `asm-dotmac-isp`. Michael's.
- `dec-isp-003` — the enforceable legacy Sub transition rule. Michael's.
- The disposition of `organizations` / `organization_memberships` rows.
- An owner for `subscribers.mrr_total`, or an explicit decision that the
  target recomputes it and the column is not migrated.
- An owner and shape for `subscribers.metadata`, or an explicit decision that
  it crosses as an opaque inventory only.
- Whether `subscriber_nin_verifications` migrates, is retained in Sub as
  regulatory evidence, or is retired.
