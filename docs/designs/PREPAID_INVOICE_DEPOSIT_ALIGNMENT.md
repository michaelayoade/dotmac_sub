# Prepaid invoicing → deposit-is-truth alignment

**Status:** Option A chosen (align prepaid to deposit-is-truth), per product
direction. The scheduled runner now treats prepaid renewal invoices as
draft-until-funded, prepaid service cuts are owned by the balance sweep, and the
phantom-AR cleanup is a data repair path rather than an enforcement input.

## Problem

Historically, for prepaid (deposit-is-truth) accounts the monthly runner
(`billing_automation.run_invoice_cycle`) created the advance/renewal invoice as
`status=issued` with `due_days=0` (due on issue). Consequences:

- The hourly `mark_overdue_invoices` (mode-agnostic) flips it to **overdue** almost
  immediately.
- It counts as **AR** in aging/overview/dashboard (`billing/reporting.py`, no mode
  filter) and **suppresses the customer's available balance** (`_resolve_prepaid_
  available_balance` = credit − open AR, `collections/_core.py:80-131`).
- It historically **opened a dunning case** when prepaid-monthly was treated as
  dunning scope. That is now explicitly forbidden: prepaid service cuts belong
  to the balance sweep, not postpaid collections.
- It is never settled from the deposit (`settle_credit_on_invoice_enabled=False`,
  disabled because account-wide settle is unsafe on migrated data), so the same
  charge is **both** an implicit wallet drawdown **and** an aged debt (double-count;
  available balance can go negative).

Prod impact (2026-07-03): **938 overdue prepaid invoices, ₦25.9M** — 673 on lapsed
(suspended) non-renewers, 224 on *active/funded* accounts (the contradiction:
positive wallet + "overdue" bill). `auto_suspend_on_overdue=False` is the only thing
preventing mass wrongful suspension today.

## Doctrine

Prepaid = **deposit-is-truth**. A renewal is funded from the customer's wallet/credit,
not chased as a receivable. A prepaid customer who does not top up simply **lapses**
(service stops via balance/expiry enforcement) — they do not accrue a debt. Therefore
a prepaid renewal invoice must not exist as AR until it is actually funded.

## Plan (Option A)

### Item 1 — draft-until-funded
At the billing boundary, for a prepaid subscription:
1. Create the invoice **`draft`** with its line items + computed total (never
   `issued`, no `due_at`). Draft is already excluded from AR, overdue-marking,
   dunning, and available-balance by contract — so drafting alone stops the pile-up
   at the source.
2. **Settle-when-funded**: if the account has enough *payment-backed* credit to cover
   the invoice total, **issue + settle that single invoice from credit** in the same
   transaction → `paid` (revenue recognized, wallet drawn down once). Uses a NEW
   **targeted single-invoice** settle (reusing `_allocatable_payments` /
   `_apply_payment_allocation` / `_project_invoice_remaining` from
   `reconcile_unposted`), NOT the account-wide `settle_open_invoices_from_credit`
   (which stays disabled — it is unsafe against migrated historical invoices).
3. If **underfunded**: leave the invoice `draft`. No AR, no overdue, no dunning. The
   customer has not renewed; service lapse is handled by balance/expiry enforcement
   (Item 2).
4. This is now the scheduled runner invariant. Runner-created prepaid invoices
   no longer support legacy issue-on-create behavior, so there is no runtime
   flag for reverting to postpaid-style prepaid AR.

### Item 2 — prepaid balance/expiry enforcement
Drafting removes prepaid AR as an enforcement input. Prepaid is instead enforced
on **balance/expiry** through `prepaid_balance_sweep`, which arms
`prepaid_low_balance_at` / `prepaid_deactivation_at`, sends prepaid-native
low-balance notices, and applies prepaid locks only when the local available
balance is insufficient. This path is independent of postpaid dunning.

### Item 3 — admin/API prepaid plan-change should draw down, not invoice ✅ DONE (this PR)
`catalog/subscriptions.py:1004-1075 _generate_proration_invoice` mints an issued
invoice for prepaid upgrades on the generic CRUD/admin path; only the portal path
(`customer_portal_flow_changes.py:709-720`) honors drawdown. Route admin/API prepaid
changes through the same `_create_prepaid_plan_change_debit` drawdown.

### Item 4 — one billing-mode authority in the read path ✅ DONE (this PR)
Dunning/enforcement use `_effective_billing_mode_for_account` (Subscription-derived,
prepaid-wins, `_core.py:173-202`); customer `build_service_status` uses
`Subscriber.billing_mode` (`service_status.py:116`). Reconcile so a drifted/mixed
account can't show a prepaid wallet while dunning treats it as postpaid.

### Item 5 — clean up the 938 phantom prepaid overdue invoices ✅ DONE (this PR — script)
Script: `scripts/one_off/cleanup_prepaid_phantom_ar.py` (dry-run by default; `--apply`;
`--unfunded-action draft|void`). Per prepaid account, oldest-first: funded invoices
(payment-backed credit ≥ balance) are settled from the deposit via the Item-1
`settle_single_invoice_from_credit`; unfunded ones are drafted (default) or voided —
removing them from AR/overdue/dunning/balance. `partially_paid` never drafted/voided
(only settled if now funded, else reported). Idempotent (stamps `metadata_`). Run the
dry-run in the prod container, review the CSV, then `--apply`. Prod estimate at build
time: ~1,101 prepaid AR invoices → ~241 funded (₦6.7M) / ~860 unfunded (₦25.9M).
Behind a `reconciliation_hold` (the existing dunning-stop flag), reclassify the
already-issued prepaid overdue/issued invoices: draft-or-void the unfunded ones,
settle the funded ones from deposit. Balance-neutral, audited (mirrors the prior
phantom-ledger cleanups). Separate one-off script, run after Item 1 lands.

## Rollout

1. Keep `prepaid_monthly_invoicing_enabled` off unless prepaid advance invoice
   rows are required. If enabled, runner-created prepaid rows stay draft until
   funded.
2. Keep `collections.prepaid_balance_enforcement` as the customer-impacting
   suspension gate; cleanup scripts must not be used as the suspension signal.
   Enabling it is a two-part launch operation: set
   `collections.prepaid_enforcement_activation_at` to the reviewed ISO-8601
   launch time and then enable the control. Adverse actions fail closed when the
   activation time is missing/invalid/not reached, while funded restoration
   remains available. The deactivation warning window is never zero (three days
   by default), and old `prepaid_low_balance_at` rows cannot become due before
   `activation_at + prepaid_deactivation_days`.
3. Run the phantom-AR cleanup in dry-run first, review the plan, then apply.
   Runtime guards exclude prepaid subscription invoices and imported/provenance
   line-less prepaid invoices from AR/dunning/balance enforcement; ambiguous
   line-less invoices remain visible and require cleanup review.
4. For migrated-account readiness, reconstruct the financial position from the
   approved Splynx cutover baseline plus native post-cutover payments, credits,
   debits, service extensions, and credit notes. Feed that named, timestamped
   snapshot to `plan_prepaid_balance_sweep.py --funding-snapshot`; the planner
   will use no local-money fallback and will still apply the production
   enforcement owner rules. Review the full planned cohort before launch.
5. **Post-change smoke test (top-up → draft settlement).** The #751 follow-ups
   (`settle_prepaid_draft_invoices_from_credit`, wired into portal top-up verify /
   webhook settlement / pending top-up reconciliation) are part of the go-live
   baseline, not optional. Immediately after the flip, exercise the path
   end-to-end: take or create ONE underfunded prepaid **draft** renewal, top up
   enough to cover it, then verify (a) it transitions to `paid`, (b) wallet credit
   is consumed **exactly once** (no double-draw), and (c) if the account was
   prepaid-suspended, service restore runs. This is the one behaviour the offline
   tests can't fully prove against real payment/webhook plumbing.

**LANDMINE:** do NOT enable `settle_credit_on_invoice_enabled` (account-wide settle)
as a shortcut — it is disabled for migrated-data safety. Item 1's settle is
single-invoice and targeted.
