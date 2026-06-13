# Prepaid drawdown billing engine

Status: design + initial implementation (2026-06-13). Inert until cutover
(`billing_enabled=false` gates the charge task). Authoritative biller remains
Splynx until the cutover runbook below is executed.

## Why

Splynx offers three billing types; DotMac has two modes:

| Splynx `customers.billing_type` | count (active) | DotMac mode | Engine |
| --- | --- | --- | --- |
| `recurring` | 344 (118) | postpaid | native invoice cycle → overdue → suspend (mature) |
| `prepaid_monthly` ("Prepaid Custom") | 25,506 (1,885) | prepaid | **this engine** |
| `prepaid` ("Prepaid Daily") | 84 (25) | prepaid | **this engine** |

~98% of the base is prepaid. Before this engine, DotMac could *read* a prepaid
balance but nothing *drew it down*: no periodic charge, so the balance never
decremented, the enforcer never tripped, and a cut-over prepaid customer would
get free service — silently, failing open. This engine closes that gap.

## Model

**Balance is ledger-based.** A subscriber's prepaid balance is the existing AR
ledger: `get_account_credit_balance` (unallocated credits − unallocated debits)
minus open invoices. This already backs native accounts and the existing
prepaid charge primitives (`_create_prepaid_plan_change_debit`, add-on debits).

- **Charge (drawdown)** → a debit `LedgerEntry` (`source=adjustment`,
  `category=internet_service`, `invoice_id=None`). No invoice; prepaid draws the
  balance down directly.
- **Top-up / renewal** → a credit `LedgerEntry` via the *existing* payment flow
  (`_create_payment_ledger_entry`). No new code: a recorded payment already
  credits the ledger.
- **Suspension** → the *existing* prepaid enforcement
  (`collections/_core.py::PrepaidEnforcement.run`) already warns → grace →
  suspends → deactivates when the resolved balance drops below `min_balance`
  (or the `collections.prepaid_default_min_balance` default). The engine only
  has to lower the balance; enforcement does the rest.

### Reconciliation with the deposit-read (#247)

`_resolve_prepaid_available_balance` returns the synced Splynx `deposit` for
Splynx-linked accounts because, in shadow mode, Splynx's net is the only truth
and the local ledger isn't seeded. That stays correct **until cutover**.

At cutover the local ledger becomes authoritative. To switch **per account
(not via a risky global flip)**, the resolver uses the ledger for a
Splynx-linked account **once an opening-balance seed exists** for it; otherwise
it keeps returning `deposit`. The seed (below) is the switch.

## Cutover sequence (runbook)

Order matters — each step is safe on its own and reversible until the last:

1. **Ship & verify classification** — PR #250 (billing_mode from
   `customers.billing_type`) + backfill. Recurring → postpaid, rest prepaid.
2. **Final deposit re-sync** — `resync_prepaid_deposits.py --execute` so local
   `deposit` matches Splynx at the cutover instant.
3. **Seed opening balances** — `seed_prepaid_opening_balance.py --execute`:
   posts a credit `LedgerEntry` (`category=deposit`,
   memo `Prepaid opening balance @ cutover`) equal to each prepaid subscriber's
   `deposit`. After this, ledger balance == deposit, and the resolver switches
   those accounts to the ledger. Idempotent (skips accounts already seeded).
4. **Enable the engine** — set `billing_enabled=true`. The daily charge task and
   prepaid enforcement (and the postpaid invoice cycle for recurring accounts)
   begin acting. Drawdown debits start reducing balances; enforcement suspends
   when they cross `min_balance`.
5. **Stop the Splynx deposit re-sync** so two writers don't fight over the
   balance (the ledger now owns it).

Recurring (postpaid) accounts can cut over independently and earlier — they ride
the existing invoice cycle and don't need the seed.

## Charge mechanics

Per active prepaid subscription whose `next_billing_at <= now` (or null):

- **Period** from `offer.prepaid_period` (free-text, previously dead):
  `daily`/`1` → 1 day; a bare integer `N` → N days; `monthly`/empty → 30 days.
- **Amount** = the recurring catalog/subscription price (via the invoice
  runner's `_resolve_price` + `_effective_unit_price`, so discounts and
  Splynx-imported `unit_price` overrides apply), pro-rated to the period:
  `round(monthly * period_days / 30)`. A 30-day period charges the full price;
  a 1-day period charges 1/30.
- Post the debit and advance `next_billing_at += period_days` in one
  transaction. Idempotent: a re-run before the next period sees
  `now < next_billing_at` and skips. Charges are *in advance* (prepaid).
- Balance is allowed to go negative (arrears) — the charge always posts, and
  enforcement suspends on the threshold. This matches Splynx (negative deposits
  exist) and avoids silently skipping a charge.

`next_billing_at` is shared with the postpaid invoice runner; safe because a
subscription is exactly one mode.

## Components

- `app/services/prepaid_billing.py` — `run_prepaid_charges(db, dry_run)` service.
- `app/tasks/prepaid_billing.py` — `run_prepaid_charges` Celery task,
  `billing_enabled`-gated, daily, `billing` queue.
- `scripts/billing/seed_prepaid_opening_balance.py` — cutover seed (dry-run
  default).
- `_resolve_prepaid_available_balance` — per-account ledger switch on seed.
- Registration: `app/tasks/__init__.py`, `app/celery_app.py` (route),
  `app/services/scheduler_config.py` (beat, gated by `billing_enabled`).

## Out of scope (follow-ups)

- Prepaid *invoices* for record-keeping (charges are ledger-only today).
- Per-customer custom period length (we read the offer's period; Splynx's
  per-customer N-days isn't migrated). Default 30 covers `prepaid_monthly`.
- Auto top-up / scheduled recharge requests (Splynx `request_auto_*`, all
  disabled in the current export).
- VAS wallet is deliberately **not** reused — it is walled off from billing.
