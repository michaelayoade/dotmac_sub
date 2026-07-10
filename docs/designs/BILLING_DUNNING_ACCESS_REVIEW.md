# Billing, Dunning, and Access Review

Date: 2026-07-10
Scope reviewed: `origin/main` billing, collections/dunning, prepaid enforcement,
subscription changes, schedulers, cleanup/reconciliation, notification guards,
and catalog permissions risk.

## Executive Position

The remaining billing risk is not one isolated bug. It is an inconsistency
between prepaid write paths and their backstop:

- customer self-service attempts to block unaffordable prepaid actions;
- admin/API plan changes can still create prepaid wallet drawdowns that push an
  account negative;
- `prepaid_balance_sweep` is the intended balance/access enforcement backstop,
  but is deliberately feature-gated off by default;
- dunning has been correctly narrowed toward postpaid AR, so prepaid AR cleanup
  and prepaid balance enforcement must be reliable on their own.

Do not blindly revert billing PRs. Reverting code may stop future behavior, but
it will not repair invoices, ledger debits, access locks, or customer balances
already written. Treat this as a billing incident map: identify the change,
classify its data effects, patch forward where records exist, and correct data
with deterministic scripts.

## Change Register

| Change | Risk area | Current assessment | Action |
| --- | --- | --- | --- |
| `#738` / `ed8629d6` prepaid balance sweep | Prepaid suspension/access | Sound design as a backstop, but default-off. Unsafe if other prepaid paths rely on it. | Keep, but add dry-run/reporting and make enablement an explicit launch gate. |
| `#741` / `b98fc621` prepaid plan-change drawdown | Admin/API prepaid upgrades | Code explicitly permits admin/API prepaid upgrades to push wallet negative and relies on enforcement. | Patch: shared affordability check plus audited override. |
| `#744` / `345263da` prepaid phantom AR cleanup | Cleanup vs enforcement | Cleanup is necessary, but dangerous if it removes evidence before balance enforcement/correction review. | Keep cleanup behind reconciliation hold/dry-run; reconcile before destructive cleanup. |
| `#751` / `dc145eb9` prepaid draft settlement/scheduler fixes | Prepaid invoice settlement | Generally supportive: settles drafts on top-up and fixes scheduler/control drift. | Keep; verify scheduled task config in prod. |
| `cb7c1248` prepaid/postpaid separation | Invoice/dunning separation | Correct strategic direction: prepaid should not run through postpaid AR/dunning by default. | Keep; expand tests around mixed accounts and imported invoices. |
| clean statement changes | Customer statement trust | Correct direction: hide internal migration/repair artifacts from customer view. | Keep; ensure admin audit remains complete. |
| scheduled change/expiry guards | Access/comms | Correct direction: canceled scheduled changes and outage-aware expiry suppression. | Keep; add billing notification suppression parity. |
| catalog permission changes | Governance | Root cause class: subscription operators got catalog mutation power. | Separate permissions and alert on billing-critical catalog edits. |

## Current Code Flow

### Invoicing

- `app.tasks.billing.run_invoice_cycle` is gated by `billing_enabled`.
- `app.services.billing_automation.run_invoice_cycle` creates invoices based on
  subscription eligibility and `next_billing_at`.
- `subscription_invoice_eligible` excludes prepaid unless explicit prepaid
  monthly invoicing is enabled.
- Prepaid monthly invoicing is intentionally opt-in through
  `billing.prepaid_monthly_invoicing_enabled`.

### Overdue and Dunning

- `app.tasks.billing.mark_invoices_overdue` marks collectible AR invoices
  overdue and emits overdue events.
- `app.tasks.collections.run_billing_enforcement` runs the unified dunning
  reconciler.
- Dunning now filters enforcement to postpaid subscriptions. This is correct:
  prepaid service cuts should be balance-based, not AR-based.
- Imported/legacy prepaid AR rows are excluded or held through invoice
  classification and reconciliation flags.

### Prepaid Enforcement

- `app.tasks.collections.prepaid_balance_sweep` calls
  `run_prepaid_balance_sweep`.
- The sweep is gated by `collections.prepaid_balance_enforcement`, with legacy
  alias `collections.prepaid_balance_enforcement_enabled`.
- The control is default-off because it can suspend customers.
- When enabled, it:
  - calculates available balance from canonical customer financial events;
  - arms low-balance timers;
  - sends warning notices;
  - suspends via prepaid enforcement locks after the deactivation window;
  - restores when balance recovers.

### Admin/API Plan Changes

- `app.services.catalog.subscriptions.update` routes plan changes through shared
  subscription update logic.
- Active cross-mode changes are rejected: prepaid cannot be changed into
  postpaid and vice versa.
- Mid-cycle prepaid upgrades create a ledger debit instead of an invoice.
- The current code comment states affordability is not gated there and that an
  admin change may push the wallet negative.

That is the most important functional gap.

## Findings

### 1. Prepaid Affordability Is Not A System Invariant

Customer self-service and admin/API paths do not enforce the same rule. A
prepaid customer can be protected in the portal but pushed negative by an admin
or API plan change.

Required invariant:

```text
A prepaid customer cannot consume paid service unless they have enough prepaid
value or an explicit approved override.
```

Fix:

- move affordability calculation into one shared service;
- use it from customer self-service, admin UI, API, and scheduled plan changes;
- reject unaffordable prepaid changes by default;
- require a dedicated override permission for exceptions;
- record override reason, actor, amount, before/after balance, and subscription.

### 2. Prepaid Sweep Is A Backstop But Is Disabled

The sweep being default-off is a reasonable safety posture during cleanup, but
the rest of the system must not depend on a disabled backstop.

Fix:

- add a dry-run command/report for `prepaid_balance_sweep`;
- show exact accounts that would be warned, suspended, restored, or skipped;
- make production enablement a launch decision after reviewing the report;
- add billing health alert when billing is live, prepaid accounts are negative,
  and the sweep is disabled.

### 3. Dunning Is Now Postpaid-Focused, Which Increases Prepaid Backstop Importance

This is strategically right: prepaid should not be collected as AR. But it means
prepaid negative balances will not be corrected by dunning. They must be handled
by prepaid affordability checks and the prepaid sweep.

Fix:

- keep dunning postpaid-only for enforcement;
- keep prepaid imported AR out of dunning;
- add prepaid balance anomaly reports independent of dunning.

### 4. Cleanup Jobs Can Hide Evidence If Run Before Classification

Phantom AR cleanup is useful, but any cleanup that voids, soft-deletes, or
reclassifies financial documents should run only after the affected balances
are classified.

Fix:

- use `reconciliation_hold` before cleanup to stop dunning/suspension;
- run expected-vs-actual reports;
- apply cleanup in reviewed buckets;
- keep admin-only audit evidence even when customer statements are cleaned.

### 5. Catalog Governance Is Still Billing-Critical

The Unlimited Basic daily/monthly incident is a catalog governance failure. A
catalog cadence or price edit can change future deductions without touching
billing code.

Fix:

- separate `subscription:write` from billing-critical catalog permissions;
- introduce `catalog:price_write` or equivalent for price/frequency changes;
- audit and alert on changes to amount, billing cycle, billing mode, duration,
  zero-price status, and recurring price rows;
- prefer catalog versioning for active plans instead of mutating live pricing.

### 6. Notification/Dunning Suppression Should Be Consistent

Expiry reminders already suppress for infrastructure-down tickets and active
outages. Billing pressure messages should follow the same customer-experience
logic where appropriate.

Fix:

- use a shared outage/infrastructure-ticket predicate for expiry, prepaid low
  balance warnings, deactivation notices, and selected dunning messages;
- do not suppress real finance/admin alerts, only customer pressure messages;
- still show the issue in the operations inbox.

## Stabilization PR Scope

Implement these before further balance cleanup:

1. Shared prepaid affordability service
   - input: account, current subscription, requested offer, effective date;
   - output: required debit, available balance, allowed, reason;
   - shared by customer portal, admin/API, scheduled changes.

2. Admin/API affordability guard
   - reject unaffordable prepaid upgrade/change by default;
   - allow override only with explicit permission and reason;
   - write audit record.

3. Prepaid sweep dry-run/report
   - list accounts by action bucket;
   - include balance, threshold, current status, active subscription count,
     open infrastructure ticket/outage suppression status, and proposed action.

4. Billing health additions
   - negative prepaid wallet count/total;
   - negative prepaid with sweep disabled;
   - prepaid active while below threshold;
   - prepaid low-balance timer armed but not suspended after due window;
   - admin/API override count.

5. Scheduler/control verification
   - verify prod values for:
     - `billing.billing_enabled`;
     - `collections.dunning_enabled`;
     - `collections.prepaid_balance_enforcement_enabled`;
     - `billing.overdue_check_enabled`;
     - `collections.billing_notifications_hourly_enabled`;
     - `catalog.scheduled_plan_change_enabled`.

6. Catalog governance guard
   - restrict billing-critical catalog edits;
   - alert on price/frequency/billing mode edits;
   - log before/after values.

## Data Review Required

Produce deterministic reports before modifying balances:

1. Negative prepaid balances
   - all active prepaid accounts below zero;
   - source of negative: plan-change drawdown, cutover debit, imported legacy
     debt, cleanup/reconciliation, manual ledger entry, payment reversal.

2. Unlimited Basic incident
   - customers on Unlimited Basic from 2026-07-06 onward;
   - catalog price/cycle history;
   - expected monthly deduction;
   - actual deductions;
   - customers kept online without sufficient balance;
   - customers over-deducted or under-deducted.

3. Dunning exposure
   - open dunning cases on accounts with any prepaid-only services;
   - overdue imported prepaid invoices still collectible;
   - suspended accounts with no valid overdue debt.

4. Scheduler exposure
   - last successful run for billing, overdue, dunning, prepaid sweep, expiry,
     scheduled plan changes, payment reconciliation, notification delivery.

## Correction Rule

Use this rule for reversal:

```text
If the change only affects future behavior, revert or patch it.
If the change already created financial records, patch forward and correct the
affected records explicitly.
```

Customer-facing statements should show real customer events: payments, service
deductions, valid credits/refunds, and valid invoices. Migration corrections,
system fixes, and cleanup evidence should remain admin-auditable but not pollute
the customer statement.

## Recommended Next Step

Open one billing stabilization PR that only changes controls and invariants:

- shared prepaid affordability check;
- admin/API guard plus audited override;
- prepaid sweep dry-run/report;
- negative-prepaid billing health metrics;
- catalog permission/audit guard for price/frequency changes.

After that PR is green and deployed, run the data review and apply balance
corrections from reviewed buckets only.
