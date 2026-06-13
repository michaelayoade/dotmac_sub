# Pre-cutover billing brief

Date: 2026-06-13. Consolidates the billing-integrity investigation and the
work that makes a Splynx → DotMac billing cutover safe. **Splynx remains the
authoritative biller; `billing_enabled=false` keeps all local billing inert
until the cutover runbook is executed.**

## 1. The model (what Splynx actually does)

Splynx offers three billing types; DotMac has two modes. Verified against the
live Splynx DB (`customers.billing_type`):

| Splynx type | UI label | count (active) | DotMac mode |
| --- | --- | --- | --- |
| `recurring` | Recurring | 344 (118) | postpaid |
| `prepaid_monthly` | Prepaid (Custom) | 25,506 (1,885) | prepaid |
| `prepaid` | Prepaid (Daily) | 84 (25) | prepaid |

**~98% of the base is prepaid.** All three settle against the customer's
**account balance = `customer_billing.deposit`**, which Splynx maintains as a
running net (invoices/payments/transactions). The deposit does **not**
reconcile to any naive local recompute (e.g. cust 25313: deposit ₦31,965 vs
payments−invoices ₦163,236), so we treat it as authoritative and never
re-derive it.

## 2. What was broken, and the fixes

| Finding | Fix | Status |
| --- | --- | --- |
| `billing_enabled` wrongly **true** in prod → local runner generated phantom prepaid invoices | flipped false; voided 12,714 unnumbered prepaid invoices | done |
| Prepaid balance/enforcement read ledger `credit − invoices`, not the authoritative `deposit` | `_resolve_prepaid_available_balance` reads `deposit` for Splynx-linked accounts | #247 merged |
| `deposit` drifted from Splynx | `resync_prepaid_deposits.py` re-synced 584 | done |
| `billing_mode` derived from a non-existent `services_internet.billing_type` → **whole base defaulted prepaid** | derive from `customers.billing_type`; backfill 207 subs / 832 subscriptions recurring→postpaid | PR #250 |
| New-customer sync hardcoded prepaid | inherits (now-correct) `Subscriber.billing_mode` | on main |
| **No prepaid drawdown engine** — nothing decremented the balance, so a cut-over prepaid customer would get free service, silently | prepaid drawdown engine (charge task + seed + ledger switch) | PR (this) |

Audit scripts committed for reproducibility: `cleanup_prepaid_phantom_invoices.py`,
`resync_prepaid_deposits.py` (PR #249), `backfill_billing_mode_from_splynx.py`
(#250), `seed_prepaid_opening_balance.py` (this PR).

## 3. The prepaid drawdown engine

Full design: `PREPAID_DRAWDOWN_ENGINE.md`. Summary: a daily Celery task posts a
per-period debit `LedgerEntry` per active prepaid subscription (period from
`offer.prepaid_period`, amount pro-rated from the recurring price); the existing
prepaid enforcement suspends when the resulting balance drops below
`min_balance`; top-ups are the existing payment-credit flow. The AR ledger
becomes the authoritative prepaid balance once an opening-balance seed exists
per account (the seed flips `_resolve_prepaid_available_balance` from `deposit`
to the ledger). All gated by `billing_enabled`.

## 4. Cutover runbook (phased, gated by billing mode)

**Recurring (postpaid, 118 active)** can cut over independently on the mature
invoice→overdue→suspend path — no seed needed.

**Prepaid (~1,910 active)** — do NOT cut over until the engine is deployed, then:

1. Ship & verify PR #250 (billing_mode) + run `backfill_billing_mode_from_splynx.py --execute`.
2. `resync_prepaid_deposits.py --execute` — deposit matches Splynx at the instant.
3. `seed_prepaid_opening_balance.py --execute` — opening-balance ledger entries
   (dry-run today: 2,384 credit ₦48.3M / 177 debit ₦515.6M / 12,694 marker).
   This switches seeded accounts to the ledger.
4. Set `billing_enabled=true` — drawdown charges, prepaid enforcement, postpaid
   invoicing, dunning and autopay all activate together.
5. Stop the Splynx `deposit` re-sync (the ledger now owns the balance).

Reversible until step 4. Run on a low-traffic window; spot-check a sample of
each type (credit / arrears / zero) before and after step 4.

## 5. Known limitations / follow-ups

- Per-customer custom period length isn't migrated; the engine reads the
  offer's `prepaid_period` (default 30d covers `prepaid_monthly`).
- Prepaid charges are ledger-only (no per-period invoice document yet).
- Auto top-up / scheduled recharge (Splynx `request_auto_*`, all disabled in the
  current export) is not implemented.
- The VAS wallet is deliberately **not** the billing balance — it is walled off.
