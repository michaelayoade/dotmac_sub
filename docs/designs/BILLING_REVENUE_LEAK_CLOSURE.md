# Billing Revenue-Leak Closure & Enforcement Consolidation

**Status:** Active remediation plan — 2026-06-24
**Author:** audit + design pass

## 1. Current Production Facts

A read-only production audit on 2026-06-24 found that DotMac Sub is the sole
biller of record. The previous billing platform is fully decommissioned; its
data remains only as historical import metadata and reconciliation evidence.

Catalog correction on 2026-06-24 reclassified the four mislabelled daily-cycle
prepaid offers as monthly. There is no genuine daily prepaid cohort in
production. Therefore:

- all active prepaid subscriptions should bill through monthly invoice-in-advance;
- invoice totals must apply VAT exclusively: `net + 7.5% VAT`.

For the ₦17,500 plan, the customer-facing invoice total is:

| Net | VAT | Gross |
| ---: | ---: | ---: |
| ₦17,500.00 | ₦1,312.50 | ₦18,812.50 |

## 2. Revenue Recovery Path

The immediate revenue fix is monthly prepaid invoicing.

1. Dry-run `run_invoice_cycle` with `prepaid_monthly_invoicing_enabled=true` and
   verify:
   - only monthly prepaid subscriptions are included;
   - VAT is applied at 7.5% exclusive;
   - proration behavior is understood before the first real run;
   - invoice count and totals match the reviewed export.
2. Run the real invoice cycle only after explicit finance/ops approval because
   it creates customer-visible invoices and AR.
3. Enable scheduled monthly-prepaid invoicing only after the manual run is
   verified.

## 3. Collections And Enforcement

Collections should be made real only after billing is producing correct invoices.

Postpaid overdue recovery can proceed independently:

- enable `overdue_check_enabled=true`;
- review `auto_suspend_on_overdue=true` blast radius before enabling;
- consider tightening the postpaid dunning policy from day-60 suspension to an
  earlier finance-approved ladder.

Prepaid enforcement remains gated until account balance truth is local-ledger
only. Do not re-enable deposit-based prepaid enforcement. The day-0 prepaid
policy is correct for pay-before semantics, but it is only safe once the
available-balance calculation no longer falls back to imported deposits.

## 4. Ledger Cleanup Gates

Before enforcement is trusted broadly:

1. Audit every active prepaid account for exactly one opening-balance seed.
2. Seed the remaining unseeded active prepaid accounts.
3. Remove the imported-deposit fallback from `_resolve_prepaid_available_balance`.
4. Add a guard that skips and alerts if an active prepaid account has no seed
   rather than falling back to imported deposit.
5. Reconcile paid invoices with non-zero balance before dashboards or restore
   logic trust `status=paid`.

## 5. Billing State Cleanup

Known cleanup tracks:

- decide whether the 118 active postpaid subscriptions on prepaid-configured
  offers intentionally remain postpaid;
- resolve paid-with-balance invoices;
- add Flutterwave keys or disable provider failover until keys exist;
- rotate live payment/secrets material and move runtime secrets to OpenBao refs;
- dedupe conflicting scheduled-task rows;
- reconcile contradictory setting rows such as text/json value drift.

## 6. Monitoring

Add billing-liveness alerts before declaring full production billing healthy:

1. Every active subscription maps to exactly one enabled billing path.
2. Invoice-cycle scan count matches eligible active subscriptions.
3. Billing, dunning, overdue, webhook, and notification runners have fresh
   successful heartbeats.
4. Billing flags match their expected values.
5. No duplicate enabled scheduled task rows exist for the same task.
6. Enforcement drift is zero: computed verdict matches service lock/RADIUS state.
7. Payments in the last 24h remain within the approved tolerance of trailing
   seven-day volume.
8. Gateway webhook dead letters, stuck top-ups, and paid-with-balance invoices
   page finance/ops.

## 7. Definition Of Done

Billing is production-complete when:

- prepaid monthly invoices are being generated with VAT;
- postpaid invoicing and dunning are active and monitored;
- prepaid enforcement reads only local ledger truth;
- customer-visible restore/suspend decisions reconcile to ledger state;
- no active runtime configuration depends on the retired billing platform;
- liveness monitors catch missing scans, failed runners, webhook backlog, and
  payment-volume collapse.
