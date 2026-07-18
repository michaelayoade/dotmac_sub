# Prepaid renewal deployment acceptance

**Date:** 2026-07-18
**Target:** explicitly approved `seabone` host
**Scope:** deployment verification and source evidence only

This packet is intentionally split into two controls:

1. prepaid access enforcement may remain enabled when its existing authority,
   readiness and activation gates are intact;
2. recurring prepaid service renewal must remain disabled until contracted
   subscription prices are complete and its dry-run is reviewed.

Deploying the code does not authorize a financial repair, baseline
supersession, control change, suspension, restoration or recurring debit.

## 1. Pre-repair acceptance command

Run inside `dotmac_sub_app` after deployment. `<REVIEWED_DEPLOY_SHA>` must be
copied from the merged release; never derive the expected value from the
running container.

```bash
python scripts/one_off/verify_prepaid_deployment_acceptance.py \
  --plan /tmp/prepaid-service-cycle-reconciliation.json \
  --expected-git-sha <REVIEWED_DEPLOY_SHA> \
  --expected-alembic-head 348_location_capture_prompt_state \
  --minimum-active-baselines 4265 \
  --expected-plan-sha256 b401456cb0d6b0bf1b23679d5eb4b008eeb2e7efd300fcec9730a5a007c20bc3 \
  --expected-entry-count 3 \
  --expected-total-amount 112875.00 \
  --expected-already-reconciled 0 \
  --expected-renewal-control disabled \
  --allow-primary
```

The command opens a read-only PostgreSQL transaction, emits aggregate JSON and
rolls back. Acceptance requires every check to be true:

- exact deployed SHA and a single expected Alembic head;
- at least 4,265 active baselines, exactly one authority cutover and exactly
  one active readiness record;
- prepaid enforcement enabled, activation valid and readiness unblocked;
- recurring renewal control still disabled;
- exact semantic plan hash, three entries, ₦112,875, zero blocked accounts and
  zero previously reconciled entries.

Any failed check stops the repair. Do not reinterpret a mismatch as harmless.

## 2. Post-repair idempotency check

After a separately approved application of the exact plan, repeat the command
with `--expected-already-reconciled 3`. All other expectations remain the same.
That proves the owner sees the three periods as existing evidence and will not
post duplicate money. It does not itself authorize a new funding baseline.

## 3. Five correction-only services

The five services are case-numbered in this document; no customer identity,
PPPoE username, source service ID or contact data is retained here.

| Case | Source correction period(s) | Source contract price | Source account evidence | Current Sub contract | Native funding evidence | Disposition |
|---|---|---:|---|---|---|---|
| C01 | 2022-01-04–2022-01-31 | ₦35,000 | 16 receipts / ₦687,100; ₦35,000 receipt near period | monthly, ₦35,000; stale 2026-07-02 anchor | no invoice line or entitlement | correction period is not recurring-cadence proof; quarantine |
| C02 | 2024-01-03–2024-01-07 | ₦35,000 | 23 receipts / ₦1,260,425; no nearby receipt | monthly, ₦35,000; stale 2026-06-28 anchor | no invoice line or entitlement | quarantine |
| C03 | 2024-12-17–2024-12-27 and 2025-02-04–2025-02-14 | ₦188,125 | 5 receipts / ₦1,949,375; no nearby receipt | monthly, ₦188,125; stale 2026-06-17 anchor | no invoice line or entitlement | two irregular correction periods; quarantine |
| C04 | 2025-09-01–2025-09-24 | ₦17,500 | 21 receipts / ₦517,500; ₦17,500 receipt near period | monthly, ₦17,500; stale 2026-06-18 anchor | no invoice line or entitlement | quarantine |
| C05 | 2026-06-11–2026-06-30 | ₦250,000 | 8 receipts / ₦3,039,625; ₦527,500 nearby | monthly, **contract price missing**, next anchor 2026-07-11 | one active issued invoice for **₦2.15m**, no entitlement | critical price/AR review; never auto-renew from catalog |

All six source rows are auto-generated category-5 `Correction` debits with
zero price, zero total and no operator comment. Source service cadence is `-1`,
tariff `billing_days_count` is null, and the periods range from five to 28 days.
They are therefore evidence of correction-covered periods, not evidence of a
monthly charge amount or a safe future billing anchor.

## 4. Recurring-price activation blocker

A read-only aggregate over the current Sub database found:

| Check | Result |
|---|---:|
| Eligible active/blocked/suspended monthly prepaid services | 4,103 |
| Missing or non-positive `Subscription.unit_price` | 120 |
| Above rows imported from Splynx | 117 |
| Above rows already due | 100 |
| Catalog amounts that would otherwise be substituted | ₦26,137,046.96 |
| Largest individual catalog fallback | ₦2,150,000 |

`financial.prepaid_service_renewals` now fails closed for those rows and reports
`prepaid_renewals_missing_price`. Payment-triggered renewal consumes the same
resolver, so confirmed money remains account credit rather than being debited
at catalog price. Historical reconciliation remains unaffected because its
reviewed plan supplies the exact amount explicitly.

Do not enable `billing.prepaid_service_renewals` until all 120 contract prices
are reviewed and materialized through the subscription owner, a read-only
full-cohort dry-run reports zero missing prices and stale anchors are separately
adjudicated. Do not copy catalog amounts into the missing fields mechanically.

## 5. Stop conditions

Stop without writes if any of the following occurs:

- deployed SHA or Alembic head differs from the reviewed release;
- authority cutover, baseline or readiness cardinality changes unexpectedly;
- enforcement readiness has a blocker;
- the three-entry plan hash, amount or count changes;
- any plan entry is blocked before repair or absent after repair;
- recurring renewal is enabled before contract-price remediation;
- a correction-only case is converted into debt, free service or an exemption
  without explicit evidence and owner review.
