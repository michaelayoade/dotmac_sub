# Legacy financial fence inventory

Status: source-derived starting inventory, with a bounded private 44-table
policy described below; not proof of complete deployed writer closure. Runtime catalog inspection and reviewed
retained-column contracts are still required by `BILLING_LEDGER_HANDOFF.md`.
This inventory does not authorize mutation, deployment, import or authority change.

## Archived monetary records and evidence

Source: `app/models/billing.py` unless a different model is named below. All
listed populations require an explicit disposition before cutover. Archive
records remain readable; this is not a deletion list.

| Population | Exact tables |
| --- | --- |
| Account ledger and credit | `billing_account_ledger_entries`, `billing_account_credit_allocations`, `billing_account_credit_allocation_items` |
| Issued receivables and closure evidence | `invoices`, `invoice_lines`, `invoice_discount_history`, `invoice_closures`, `invoice_closure_ledger_evidence` |
| Credit documents | `credit_notes`, `credit_note_lines`, `credit_note_applications` |
| Cash and settlement | `payments`, `payment_refunds`, `payment_reversals`, `payment_settlements`, `payment_allocations` |
| Legacy financial position | `ledger_entries`, `account_adjustments`, `billing_runs` |
| Consolidated reconstruction evidence | `consolidated_credit_consumption_reconciliation_evidence`, `consolidated_payment_return_allocation_evidence`, `consolidated_payment_return_reconciliation_evidence`, `consolidated_payment_return_document_reconstruction_evidence`, `consolidated_payment_settlement_reconciliation_evidence` |
| Allocation and bank reconciliation | `payment_allocation_reconciliation_exceptions`, `bank_reconciliation_runs`, `bank_reconciliation_items` |
| Local subledger (`customer_subledger.py`) | `customer_subledger_opening_positions`, `native_prepaid_opening_repairs`, `customer_subledger_opening_corrections`, `customer_subledger_authority_cutovers`, `customer_posting_groups`, `customer_position_effects` |
| Prepaid reconstruction (`prepaid_funding.py`) | `prepaid_funding_reconstruction_batches`, `prepaid_funding_baselines`, `prepaid_opening_funding_consumptions`, `prepaid_funding_trigger_executions`, `prepaid_funding_trigger_subscription_outcomes`, `prepaid_activation_funding_overrides`, `prepaid_draft_reconciliation_exceptions` |
| Historical import explanations | `payment_import_batch_reversals`, `payment_import_batch_reversal_items` (`imports.py`), `cutover_balance_variances` (`cutover.py`), `splynx_billing_transactions` (`splynx_transaction.py`) |

`import_runs` and `import_run_rows` also explain original import and reversal
sources. Their retained operational uses need classification; they cannot be
blanket-frozen merely because historical financial rows reference them.

## WHT, proofs and derived evidence

`app/models/payment_proof.py` declares `payment_proofs`,
`payment_proof_corrections`, `withholding_tax_records` and
`withholding_tax_transitions`. These contain gross/net/WHT/VAT amounts, certificate
and invoice/payment/reversal references. WHT facts must transfer to their new
owner. Proof submission/review remains a product flow; its retained contract must
not preserve a parallel settlement writer. Historical correction and lifecycle
records remain evidence. Tax rates/bases and customer tax policy are separate
configuration, not a second money authority.

Derived populations need explicit projection/rebuild/archive dispositions:
`billing_receivable_projections`, `receivable_projection_runs`
(`billing_receivable_projection.py`), `billing_cutover_verification_runs`,
`billing_shadow_delivery_evidence` (`billing_shadow_verification.py`), and
`prepaid_coverage_reconciliation_runs`, `prepaid_coverage_reconciliation_items`
(`prepaid_coverage.py`). Their continued observational use does not authorize
legacy repairs or financial postings.

## Shared product state: classify columns, do not blanket-freeze

| Table or population | Required boundary |
| --- | --- |
| `billing_accounts` | `balance` loses independent ledger authority; retain identity, reseller, routing and configuration. Any displayed balance becomes a named projection. |
| `subscribers` | Classify `deposit`, `min_balance`, `prepaid_low_balance_at`, `mrr_total` separately: position/projection, commercial policy and notification state have different owners. Retain account/product lifecycle. |
| `topup_intents` | Keep module correlation, requested/actual amount observations and lifecycle inputs. Settlement posts through Payments/Billing only. Migration 691's insert/update/delete correlation guard remains relevant. |
| Prepaid purchase/period/entitlement populations | Preserve exact pricing/tax/period snapshots and invoice links as product or historical evidence; new issuance must refer to the new monetary owner. |
| Subscription billing arrangements/grants | Approved maximum/reference amounts are terms and policy snapshots, not standalone ledger balances. |
| `payment_provider_events` | Amount/fee/net fields are normalized external observations, not posting authority. |
| `customer_tax_policies` | WHT enablement, exemption and version are policy inputs. |
| `erp_billing_exports` | Retain committed-fact delivery/replay/ack transport; ERP export status cannot become a second ledger. |
| Sales quotes/orders | Price/quote authorization remains Sales. Order amount-paid/balance-due/paid-at fields need an explicit Billing-fed projection/rebuild contract. |
| Provider/channel/collection configuration | Retain routing/configuration; do not freeze as monetary history. |

Review migration 691's existing `UPDATE(id)` grants on `invoices` and
`subscribers`: these are effective DML, not harmless lock permissions. Final
fencing must remove or replace them under an explicit retained-owner contract.

## Database-side closure still required

Review actual deployed triggers/functions against source migrations, including:

- `483_invoice_discount_history.py`: immutable discount history.
- `290_wht_lifecycle_timeline.py`: append-only WHT transitions.
- `598_customer_subledger_opening_corrections.py` and
  `624_native_prepaid_opening_repairs.py`: correction/repair evidence guards.
- `558_receivable_projection.py`: interpolated monotonic projection guard and
  trigger names; source text alone cannot attest installed catalog objects.
- `393_prepaid_coverage_reconciliation.py`: reconciliation evidence guards.
- `691_payments_intent_correlation.py`: retained topup correlation guard.
- `399_subscription_billing_treatments.py`: retained product grant/term guards.

Inspect table/column grants, owners, PUBLIC, inherited/assumable roles,
partitions/inheritance, views/rules, triggers and reachable privileged helper
functions. Preserve legitimate infrastructure such as outbox delivery while
closing monetary laundering paths. Unclassified dynamic SQL, WHT, projection,
reconstruction and reconciliation writers remain activation blockers.

`docs/FINANCIAL_OWNERSHIP_RETIREMENT.md` explicitly bounds its AST census; it
cannot prove dynamic SQL or reflection absent. No live catalog, database or
runtime was inspected to produce this inventory. The final executable policy
must enumerate exact objects and retained columns from reviewed source plus
actual catalog evidence, and must fail closed on an unknown object or role path.

## Private archive component v1

`ArchiveRelation` in `legacy_archive_fence_contracts.py` enumerates exactly the
44 tables in the first section above. `_install_legacy_archive_fence` stages
only that closed archive component inside a future registered handoff coordinator.
It has no production caller, adapter, CLI, migration or startup wiring. All
retained/shared/module/outbox/provider/proof/WHT/configuration/ERP and derived
populations remain outside this component; their contracts remain unresolved.

The actual non-superuser current role must own every archive table and public
namespace. `app_user` must be nonowner and have no effective MEMBER/USAGE/SET
path to that actual owner. All tables must be ordinary, unpartitioned,
noninherited and unshadowed. Deterministically acquire all 44 ACCESS EXCLUSIVE
locks before any change, using positive owner-supplied lock timeout <=60 seconds
and statement timeout <=120 seconds. The same untranslated Engine/connection
must own the clean explicit READ WRITE READ COMMITTED root; Session/Connection
savepoints and external roots refuse. The whole future owner MUST prohibit
manual SQL SAVEPOINTs, which this participant cannot reliably introspect.

One fixed owner-controlled SECURITY INVOKER function with pg_catalog search path
unconditionally denies mutation. ALWAYS row INSERT/UPDATE/DELETE and ALWAYS
statement INSERT/UPDATE/DELETE/TRUNCATE guards preserve all existing guards.
Revoke table DML and column INSERT/UPDATE, including invoice UPDATE(id), and
grant options from PUBLIC and every observed nonowner grantee, preserving SELECT.
RESTRICT revocation refuses dependent grant chains; it never uses CASCADE.
Reinspect actual catalogs. Exact replay verifies fixed function/trigger identities,
body, arguments, owner, events and enabled state and absence of mutation ACLs;
collisions, partial installation and drift refuse without repair or unfreeze.

A typed `billing.legacy_archive_fence.staged` audit output is staged through the
existing owner-output protocol only for a new installation. It binds policy
revision, 44-table count, catalog digest and the existing owner command identity.
It commits or rolls back with the caller's root; exact replay emits no duplicate.
This is neither a global installation receipt nor activation evidence. The
returned observation is transaction-local staged catalog evidence. Complete
helper/view/FK/privileged-function closure, other runtime role paths and every
retained-column population remain global handoff blockers. Authored hosted
canaries have not been executed locally and test-only clone role provisioning
does not prove production posture.

## Private retained-column component v1

The existing `runtime.billing_legacy_archive_fence` owner also contains the
private `_install_legacy_retained_columns_fence` participant. Its closed policy
is exactly `billing_accounts.balance` and `subscribers.deposit` (numeric(12,2),
existing UUID primary keys). It reuses that owner's root, role and bounded-wait
proof, requires the same physical audit/owner connection and locks both ordinary,
unpartitioned, noninherited tables before mutation and reobservation. The final
handoff coordinator must prelock the sorted global union before final observation;
component-local locks alone do not establish final population capture.

Account insertion requires exactly zero balance; subscriber insertion requires
NULL deposit. UPDATE preserves the exact NULL-sensitive value and primary key.
DELETE permits only zero account balance or NULL subscriber deposit: a historic
subscriber deposit of zero is recorded history and cannot disappear. Existing
FK/lifecycle checks remain. This deliberately narrows the existing hard-delete
owner's deposit-row behavior; soft lifecycle transitions remain available.
AFTER INSERT/UPDATE row guards check the final tuple even when another BEFORE
trigger changes it. ALWAYS DELETE guards and statement TRUNCATE guards also apply
in replica mode. Functions are fixed SECURITY INVOKER with pg_catalog search path,
direct OLD/NEW comparisons and no joins or RLS-dependent lookups. The component
revokes only nonowner/PUBLIC TRUNCATE, including grant options, using RESTRICT;
shared-table DML and column grants remain usable and row guards enforce policy.
Exact catalog replay emits no duplicate transactional
`billing.legacy_retained_columns.staged` audit; partial installation, body, timing,
arguments, ownership, enabled-state or TRUNCATE ACL drift refuses without repair.

`min_balance` remains commercial policy, `prepaid_low_balance_at` notification
state and `mrr_total` a writable cache. Its current unregistered MRR writer is
RETIRE_AFTER_CUTOVER; a registered Billing-fed balance display owner and explicit
MRR disposition remain final handoff gates. This component does not bless these
legacy writers permanently, classify privileged helper closure, seal, write the
installation receipt, activate, commit, grant module DML or unfreeze writers.
There is no production caller, migration, CLI or startup wiring. Hosted migrated
PostgreSQL canaries are authored; no local database/runtime execution or production
acceptance is claimed.
