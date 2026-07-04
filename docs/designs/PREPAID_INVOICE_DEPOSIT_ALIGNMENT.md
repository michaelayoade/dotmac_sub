# Prepaid invoicing → deposit-is-truth alignment

**Status:** Option A chosen (align prepaid to deposit-is-truth), per product
direction. Item 1 done (#734, flag OFF), Item 2 done (#738, control OFF), Items 3 & 4
done (this PR). Item 5 (data cleanup) + the coordinated flag flip remain.

## Problem

For prepaid (deposit-is-truth) accounts the monthly runner
(`billing_automation.run_invoice_cycle`) creates the advance/renewal invoice as
`status=issued` with `due_days=0` (due on issue) — the status is hardcoded, not
conditioned on billing mode (`billing_automation.py:~1327-1338`). Consequences:

- The hourly `mark_overdue_invoices` (mode-agnostic) flips it to **overdue** almost
  immediately.
- It counts as **AR** in aging/overview/dashboard (`billing/reporting.py`, no mode
  filter) and **suppresses the customer's available balance** (`_resolve_prepaid_
  available_balance` = credit − open AR, `collections/_core.py:80-131`).
- It **opens a dunning case** (prepaid-monthly is in dunning scope while
  `prepaid_monthly_invoicing_enabled` is on — it is, in prod).
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

### Item 1 — draft-until-funded (this PR)
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
4. **Flag-gated** behind `billing.prepaid_draft_until_funded` (default **OFF**) so prod
   behavior is unchanged until enabled deliberately after review — matching how this
   codebase ships risky billing changes (prepaid engine deployed inert, etc.). The
   flag is read by the create path (satisfies the no-orphan-settings lint).

### Item 2 — revive balance/expiry prepaid enforcement (next)
Drafting removes today's only prepaid enforcement (day-0 suspend on the issued
invoice). Prepaid must instead be enforced on **balance/expiry**: a sweep that arms
`prepaid_low_balance_at` / `prepaid_deactivation_at` (currently only ever *cleared* —
`_core.py:1958-1972`; the balance engine was retired in #376), wired to the 8 orphan
`prepaid_*` settings (low-balance warning subject/body, deactivation days/subject/body,
skip weekends/holidays, blocking time) as its config → resolves the earlier
wire-vs-delete question to **WIRE**. Emits the prepaid-native low-balance warning that
`service_status.py:121-137` already renders but never receives.

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

1. Ship Item 1 flag-OFF; verify in the test stack (draft-when-unfunded, issue+paid
   when funded, no double-charge, idempotent, balance draws down once).
2. Ship Item 2 (balance/expiry enforcement) so prepaid is still enforced once
   invoices stop being the enforcement trigger.
3. Enable `prepaid_draft_until_funded` in prod **only after** Item 2 is live and the
   Item 5 cleanup of the existing 938 is done — otherwise drafting new invoices while
   old ones remain overdue leaves a mixed state, and enabling without Item 2 removes
   prepaid enforcement entirely.
4. Items 3 & 4 are independent hardening; ship anytime.

**LANDMINE:** do NOT flip `prepaid_monthly_invoicing_enabled` off/on or enable
`settle_credit_on_invoice_enabled` (account-wide settle) as a shortcut — the latter is
disabled for migrated-data safety. Item 1's settle is single-invoice and targeted.
