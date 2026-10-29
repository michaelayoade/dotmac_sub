# Tax policy authoring workbench

This is a source implementation dependent on public **dotmac-tax 0.1.0a7**
(candidate in Starter, unpublished at implementation time). It does not claim
package release, executable acceptance, production publication or activation.
Billing a9 is independently required by the native invoice consumer. The root
integrator owns release verification, exact pins/lock and shared registration.

## Page contract and authority

Screen `billing.tax_policy_workbench` is a staff finance policy-authoring
workbench, with bounded catalog pickers and history investigation panels. It is
not a generic numbered list screen. The public Tax cursor contract owns ordering,
page bounds and opaque cursors; no numbered PageMeta, total or offset is invented.
The default bound is 25, maximum 100. The next link retains the current filter
and independent catalog/history cursor. Totals are explicitly unavailable.

The first viewport identifies current persisted policy, observation time, its
future-determination impact, primary initial publication and the investigation
path. Catalog rows show code/name, state, context and the next scoped picker;
UUID evidence is disclosed on demand. Authorities narrow jurisdictions, which
narrow codes; a code opens published version history. Empty selections are
explicit. A next cursor means partial coverage, not an inferred total. Invalid
or foreign cursor/identity refuses; it never restarts silently. Refresh after
stale evidence. No cached read model or repair writer is introduced.

`financial.tax_policy_authoring` owns the typed workbench and atomic application
coordination. Public Tax owns catalog validation, effective version selection,
all rate/base/method/band mathematics, immutable versions, predecessor and
supersession provenance. Its public DTOs are the only Tax read boundary.
Sub does not import Tax models, issue ad hoc Tax SQL, write policy during invoice
issuance, or choose a national rate. Currency precision is explicit and an input
that would be rounded is refused before constructing Money.

Initial rule publication accepts version 1 only. Further versions require the
explicit supersession command, showing predecessor UUID/version, replacement
version/effective dates, reason, evidence and source identity/revision. The
history row can seed the exact predecessor and code fields through public
`get_tax_rule`; rates and replacement dates are never inferred. Historical rules
have no edit/delete action. Tax itself checks the predecessor version and leaf,
code identity, effective ordering and exact replay content at execution.

The author screen uses one shared ActionForm with declared typed fields,
confirmation, impact and retained validation errors. The canonical renderer
provides CSRF and emits `confirmed=yes`. Boolean inclusivity, rates, recognition,
bases, sequence and treatments are explicit inputs, without a preselected rate
or national policy. Progressive bands use a bounded JSON field parsed into typed
BandDraft/TaxRuleBandInput values (max 100), never a free-form domain bag.
Publication adapters accept no manually entered actor or approver.

Persisted-policy preview is a separate read operation with synthetic input
facts. It calls `preview_tax_policy`, creates no determination, and never
simulates an unpublished draft. It shows net/tax/gross, currency and component
base, treatment, rule/version and recovery amounts. It is advisory current
policy, not durable invoice evidence. A missing matching policy is a refusal,
not zero. The billing settings link goes to the existing audited native binding
setting; no duplicate binding writer or switch activation is introduced.

Desktop and mobile consume the same owner contracts. Tables scroll horizontally
on narrow screens; forms stack labels, retain values and associate errors with
fields through the shared renderer. History/identity evidence uses details.
Unauthorized access is refused by canonical gates; publication forms have a
separate guarded GET. Read-only users can investigate and preview. Canonical permission-owner queries
control publication-link visibility; the write route gate still rechecks
permissions and staff-leave restrictions before accepting commands.

## Transaction, locking and replay

Each command carries CommandContext, server-produced PermissionAdmission, typed
payload and confirmation. Routes use keyword arguments and end only their read
transaction before invoking the owner. `execute_owner_command` is entered once
on a transaction-free session. The owner requires `system_user` and exact
`billing:tax:write` admission, validates context attribution, locks the canonical
staff principal and checks active state. Its label resolves through the staff
provisioning owner's typed display query; canonical Party enriches audit only.
The lock precedes Tax's catalog/code locks. No credential query is needed.

Tax flush-only ensure/supersede commands participate in that same root;
`stage_audit_event` stages the canonical audit record atomically. Audit failure
rolls back Tax publication. This coordinator creates no independent Sub Tax
policy/event stream. Tax remains the policy record owner, audit remains the
canonical attempt/provenance owner. Every accepted exact replay records its own
staff audit attempt and can return the existing policy identity.

Catalog retry identity is Tax's tenant/natural identity and exact current
content; source references are audit provenance, not a global deduplication
ledger. Immutable initial rules replay by tenant/code/version plus exact policy
content. Supersession replay is Tax's exact source_ref/source_version identity
and material content, and remains valid after subsequent replacement. A source
revision is not an authorization credential. Changed inputs require explicit
review and a new source revision; they cannot repurpose an existing publication.
A stale predecessor never auto-rebases. Refresh history, review the new leaf,
and explicitly resubmit its predecessor/version and effective dates. Database
integrity/serialization failure requires retrying the whole root on a fresh
session; no failed transaction is queried for a winner.

## Root integration and qualification

The integrator must:

- Verify Tax a6 release first, then a7 (including verified V2 result lookup),
  then exact-pin a7 and regenerate lock; do not claim a
  candidate source version is published. Billing a9 is independently required.
- Include `app.web.admin.billing_tax_policy.router` under `/admin` with the same
  billing module guard used by billing reporting.
- Reuse existing `billing:tax:read` and `billing:tax:write` permissions and add
  the new routes to canonical route authorization declarations. No new seeds.
- Import `SERVICES` from the new `financial_access/tax_policy_authoring.py`
  capability shard into the existing financial_access domain, then regenerate
  SOT relationship documentation through its owner generator.
- Run required hosted lint/type/import/security/architecture suites, focused
  unit and route tests, and migrated PostgreSQL canaries. No dependencies or
  tests were installed/executed locally. No local Docker is authorized.
- Run desktop/mobile browser verification for workbench, bounded next pages,
  history, field errors/confirmation, permission-denied and missing-CSRF flows.
  Source template parsing is not browser acceptance.

Focused tests author exact replay, stale predecessor, effective boundary date,
public bounded history, canonical actor attribution and same-root audit rollback.
Fixed/progressive publication and preview canaries are authored as well.
Hosted race/serialization qualification remains required before merge; no deployment acceptance claimed.
The repository real migration chain owns the PostgreSQL schema, including Tax
supersession migrations. No new Sub migration or policy table is introduced.

## Review corrections

Every rule method, including percentage/progressive and every supersession,
checks explicit code/jurisdiction natural identities through public Tax getters.
Both returned UUIDs must match the submitted selected identities. Submitted
currency code and minor-unit precision must equal that jurisdiction before any
publication or audit is staged; nonfixed inputs cannot silently acquire different
units. Guided catalog/history links seed these identity fields and catalog currency
from bounded selections; incomplete direct links show required fields without
scanning catalogs or guessing identities.

Successful publication returns typed scope. Its owner projection redirects to
the new authority's jurisdiction picker, the jurisdiction's code picker, or the
exact code's history for codes/rules/supersessions. No untrusted success message
is appended to the URL. Dates and observation timestamps use app_datetime.

Publication scope also retains the public owner's jurisdiction/code natural
identities. Redirects and subsequent Inspect/author links preserve these alongside
UUID scope, so returning from a successful publication retains guided identity
and currency seeding. Code creation supplies its jurisdiction natural code and
checks the public getter's UUID before writing; no catalog scan is used.

## Explicit cross-purpose dependency authoring (Tax a7)

The consumer now requires verified Tax **a7**, including V2 publication,
supersession, bounded history and exact scoped V2 result lookup. Source a7 is a
candidate, not a release claim. Tax a6 must release first; this UI must then pin
verified a7 rather than the a6 authoring surface. No dependency pin/lock or live
activation is performed by this slice.

RuleDraft makes cross-purpose dependencies explicit: selection `none`,
`all_prior`, or `tax_code_ids`; one complete prior signature (jurisdiction,
fact kind, recognition basis, transaction side); and bounded selected code UUIDs.
Public TaxBaseDependencyPolicyV1 validates selection/codes. Public Tax V2
publication validates `source_plus_dependency_tax` against that policy and owns
all base/rate/band mathematics. No product service adds prior tax to an amount.
No-dependency rules retain their original exact fields and use V2 with no
external dependency policy. Initial and supersession publication, staff/currency
checks, locking, atomic audit and exact replay remain the same owner boundary.

History uses public ListTaxRuleHistoryV2/list_tax_rule_history_v2 and displays the
complete dependency signature, selection and selected codes for each immutable
version. The V1 rule getter is used only for exact predecessor identity/version;
it is never used to pretend a dependent rule has no dependency. A replacement
requires an explicit new dependency choice; no inferred rate or prior purpose.

The bounded code picker carries a typed target identity and optional predecessor
version. Operators add up to 100 code UUIDs from bounded pages, then choose
selected codes or explicitly choose all prior components for the shown prior
jurisdiction. Its author link seeds that target's canonical identity/currency,
selection, code UUIDs, prior jurisdiction and required dependency base. Operators
complete the remaining prior signature fields. The declared dependency is
submitted to Tax unchanged. Untrusted picker/query values never authorize a
policy write; canonical Tax IDs, currency and policy admission are checked again.
No whole-catalog scan or new private Tax read path is introduced.

## Verified dependent preview

The preview form accepts bounded actual prior determination-set UUIDs, never
entered fingerprints, tax amounts or invented prior source references. The
read owner resolves each through public
`get_tax_determination_set_v2(db, tenant_id=OPERATOR_TENANT_ID,
determination_set_id=...)`. That owner getter refuses missing/V1/unsealed/corrupt
results and verifies the persisted V2 seal; the UI does not duplicate its seal
algorithm or interpret private storage. Exact references and original commercial
source identity are copied from its public immutable DTO. Prior results with
different commercial identities refuse. TaxFactV2 carries the original input
commercial amount unchanged plus those actual references into public
`preview_tax_policy_v2`; Tax validates matching source context and selects the
persisted policy. No determination is created and no policy is published.

The preview result shows the verified commercial identity, exact prior result
references under evidence disclosure, selected dependency policy and component
evidence. A current/V1 preview that meets dependent persisted policy refuses with
an actionable instruction to supply actual prior V2 evidence; there is no V1
fallback or synthetic prior result. Missing UUIDs, foreign/V1 evidence, corrupt
seals or incompatible current facts remain refusals. The public getter enables
exact-UUID lookup, not unbounded search or a new evidence discovery index.

Tests authored for hosted acceptance cover no-dependency V2 fidelity, complete
signature/selection validation, bounded picker target/predecessor preservation,
UUID-only evidence input, missing/V1 preview refusal, actual migrated-PG V2
publication/history/supersession, exact retries and verified prior-result preview
without amount preaddition. Static validation uses the a7 source on MYPYPATH;
none of these tests were run locally. Full hosted CI, migrated schema including
Tax a7's migration, module release/pin validation and browser acceptance remain
mandatory before merge/use.
