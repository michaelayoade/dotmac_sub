# Billing, Dunning, and Access Review

Date: 2026-07-15
Scope reviewed: `origin/main` billing, collections/dunning, prepaid enforcement,
subscription changes, schedulers, cleanup/reconciliation, notification guards,
and catalog permissions risk.

## Executive Position

The review originally found an inconsistency between prepaid write paths and
their backstop. The feature branch now closes the immediate plan-change and
catalog-governance gaps:

- customer self-service, admin/API updates, and change requests use one prepaid
  plan-change decision;
- the account is locked and the decision is recomputed before the debit/credit
  and subscription mutation are committed together;
- plan-change adjustments use a stable idempotency key;
- billing-critical catalog mutations require `catalog:billing_write`, are
  audited/observed, and cannot edit live pricing or cadence in place;
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
| `#741` / `b98fc621` prepaid plan-change drawdown | Admin/API prepaid upgrades | Closed on this branch: every immediate human path confirms the owner fingerprint and idempotency key; locked execution recomputes, rejects stale/insufficient/debt state, and links the request to its exact adjustment or credit note and ledger row atomically. Immediate bulk is gated; next-cycle bulk has no immediate money. | No bypass flag. Refresh the preview, fund prepaid service, post a valid owner credit/discount, or schedule next-cycle. |
| `#744` / `345263da` prepaid phantom AR cleanup | Cleanup vs enforcement | Cleanup is necessary, but dangerous if it removes evidence before balance enforcement/correction review. | Keep cleanup behind reconciliation hold/dry-run; reconcile before destructive cleanup. |
| `#751` / `dc145eb9` prepaid draft settlement/scheduler fixes | Prepaid invoice settlement | Generally supportive: settles drafts on top-up and fixes scheduler/control drift. | Keep; verify scheduled task config in prod. |
| `cb7c1248` prepaid/postpaid separation | Invoice/dunning separation | Correct strategic direction: prepaid should not run through postpaid AR/dunning by default. | Keep; expand tests around mixed accounts and imported invoices. |
| clean statement changes | Customer statement trust | Correct direction: hide internal migration/repair artifacts from customer view. | Keep; ensure admin audit remains complete. |
| scheduled change/expiry guards | Access/comms | Correct direction: canceled scheduled changes and outage-aware expiry suppression. | Keep; add billing notification suppression parity. |
| catalog permission changes | Governance | Closed on this branch with `catalog:billing_write`, durable audit, observability/admin alerting, duplicate-price guards, and live mutation guards. | Grant narrowly; use offer versions for pricing/cadence changes. |

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
  - resolves the same account → policy → billing-mode grace decision as
    dunning and customer service status;
  - suspends via prepaid enforcement locks after that grace deadline;
  - restores when balance recovers.

### Grace and Network Access Tier

- `app.services.collections.grace_policy` owns grace duration, provenance,
  deadline, and elapsed post-grace days. Dunning steps are relative to grace
  end; prepaid planning/enforcement/status consume the same decision.
- `EnforcementLock.access_mode` persists whether a restriction requests hard
  reject or captive. Financial consequence evidence stores the effective mode.
- Hard reject is the default. Captive requires an explicit direct-house
  residential opt-in plus an enabled, valid portal IP/CIDR and HTTPS URL.
  Business, reseller, system, uncategorized, and otherwise ineligible accounts
  fail closed even when a stale opt-in flag exists.
- RADIUS population, event sync, access-state shadowing, and connectivity
  planning re-resolve the persisted mode through
  `app.services.walled_garden_policy`; none decides from the raw flag.
- Keep the global captive setting disabled until staging verifies RADIUS
  readback, portal reachability from the restricted tier, one real payment,
  and canonical restoration after settlement. A failed readiness check always
  resolves to hard reject.
- `PolicySet.suspension_action` is legacy compatibility data only. Dunning
  steps own post-grace actions, and the admin form no longer writes the legacy
  selector.

### Admin/API Plan Changes

- `app.services.catalog.subscriptions.update` routes plan changes through shared
  subscription update logic.
- Active cross-mode changes are rejected: prepaid cannot be changed into
  postpaid and vice versa.
- Mid-cycle prepaid upgrades create a ledger debit instead of an invoice.
- Mid-cycle downgrades stage the valid credit note in the same transaction.
- Scheduled next-cycle changes skip immediate proration because no current-cycle
  service value is being purchased.

## Findings

### 1. Prepaid Affordability Is A System Invariant

Customer self-service, admin/API updates, and approved change requests now
enforce the same rule through `app.services.prepaid_plan_changes`.

Required invariant:

```text
A prepaid customer cannot consume paid service unless they have enough prepaid
value.
```

There is no generic admin override. Exceptions must be represented by a real
payment, approved credit/discount, or a scheduled next-cycle change.

### 2. Prepaid Sweep Is A Backstop But Is Disabled

The sweep being default-off is a reasonable safety posture during cleanup, but
the rest of the system must not depend on a disabled backstop.

Implemented readiness layer:

- `plan_prepaid_balance_sweep.py` reports the exact warn, suspend, restore,
  deferred, shielded, health-blocked, invalid, and no-op cohorts;
- each row includes the canonical balance/threshold, parent-status projection
  drift, active service/lock counts, and outage/ticket notice suppression;
- the report and executor consume `prepaid_enforcement_planner`; planning has no
  timer, notification, service-state, or network side effects;
- the planner accepts a named, timestamped, complete reconstructed-funding
  snapshot for migrated accounts and never fills missing rows from the local
  ledger, while the enforcement owner still selects the cohort and applies all
  non-money policy;
- launch requires an explicit activation timestamp and a non-zero warning
  window (three days by default); stale timers are floored at activation so
  enabling the control cannot immediately suspend an old cohort;
- production enablement remains a launch decision after reviewing the report;
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

### 5. Catalog Governance Is Billing-Critical

The Unlimited Basic daily/monthly incident is a catalog governance failure. A
catalog cadence or price edit can change future deductions without touching
billing code.

Implemented:

- `subscription:write` remains separate from `catalog:billing_write`;
- amount/currency/cycle/mode/term/tax and active price changes are audited and
  surfaced through observability/admin alerts;
- duplicate active recurring prices are rejected;
- live offer, offer-version, and add-on pricing/cadence mutations are rejected
  with a version/new-catalog-entry migration instruction.

### 6. Notification/Dunning Suppression Should Be Consistent

Expiry reminders already suppress for infrastructure-down tickets and active
outages. Billing pressure messages should follow the same customer-experience
logic where appropriate.

Fix:

- use a shared outage/infrastructure-ticket predicate for expiry, prepaid low
  balance warnings, deactivation notices, and selected dunning messages;
- do not suppress real finance/admin alerts, only customer pressure messages;
- still show the issue in the operations inbox.

## Stabilization Status

Complete or verify these before further balance cleanup:

1. Shared prepaid affordability service - implemented
   - input: account, current subscription, requested offer, effective date;
   - output: required debit, available balance, allowed, reason;
   - shared by customer portal, admin/API, and change-request application;
   - scheduled next-cycle changes intentionally create no immediate adjustment.

2. Admin/API affordability guard - implemented
   - reject unaffordable prepaid upgrade/change by default;
   - no generic override; fund/credit/discount or schedule the change;
   - lock, recompute, and commit the adjustment with the subscription.

3. Prepaid sweep dry-run/report - implemented by
   `scripts/one_off/plan_prepaid_balance_sweep.py`.

4. Billing health additions
   - negative prepaid wallet count/total;
   - negative prepaid with sweep disabled;
   - prepaid active while below threshold;
   - prepaid low-balance timer armed but not suspended after due window;

5. Scheduler/control verification
   - verify prod values for:
     - `billing.billing_enabled`;
     - `collections.dunning_enabled`;
     - `collections.prepaid_balance_enforcement_enabled`;
     - `billing.overdue_check_enabled`;
     - `collections.billing_notifications_hourly_enabled`;
     - `catalog.scheduled_plan_change_enabled`.

6. Catalog governance guard - implemented
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

Keep this branch undeployed until its focused and full suites are green. After
review and deployment, verify the new permission exists before granting it,
exercise one test plan change and one catalog-version creation, then continue
the separately controlled prepaid-enforcement rollout. Account corrections must
still come from reviewed financial evidence, not from this code deployment.
