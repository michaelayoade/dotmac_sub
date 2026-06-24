# VTU / Bill Payments (VTPass) — Design

Status: **DRAFT — decided model, not yet scheduled for build** (2026-06-12)

## Decisions (made)

1. **Audience: customers AND resellers**, day one. Resellers transact for
   walk-in customers; the platform owner profits from customer-direct
   transactions plus a deferred override on reseller volume.
2. **Economics: deferred-override model.** Resellers get the full current
   VTPass rate up front (zero owner margin on their transactions initially).
   Combined volume tiers the owner up VTPass's commission ladder; the
   override = `owner_vtpass_rate − reseller_fixed_rate` appears and widens
   over time without touching what resellers earn. Reseller statements show
   only their fixed rate — the override is invisible to them **by design and
   by test** (see Invariants).
3. **Funding: single customer-facing Wallet ("the Wallet"), with DotMac as a
   biller.** The new VAS wallet becomes the ONE balance customers fund and
   see. The existing service credit balance remains internal (what invoices
   settle against) and stops being a customer-facing concept. Paying the
   DotMac internet bill is a biller entry in the same bill-pay UI: it moves
   wallet money into the billing side as a normal `Payment` (identical to a
   gateway webhook payment) — one-way; reversals use the existing
   refund/credit-note path. **Auto-deduct is an opt-in toggle** (sweep at
   invoice due date, receipt notification, falls through to existing
   dunning/card-autopay when short; default OFF). Collections/enforcement
   only ever see money once it is moved — the wallet is consent-gated by
   construction. Existing service credit balances do NOT migrate; the wallet
   starts at zero. Resellers get a VAS float wallet with identical
   semantics; commission is credited to it **on confirmed delivery only**.
4. **Segregated wallet bank account.** Wallet top-ups settle into a
   dedicated bank account (gateway subaccount / second integration key),
   separate from service revenue — wallet balances are customer liabilities,
   not revenue. Invariant, checked by a daily three-way reconciliation
   (wallet ledger ↔ bank settlement report ↔ VTPass float):
   `bank + float ≥ Σ wallet liabilities`. Money becomes ours only when spent
   (VTU delivered / DotMac bill paid), matched by an internal transfer.
   **No customer cash-out/withdrawals in v1** (manual support refunds only)
   — withdrawals are the e-money tipping point and wait on the regulatory
   check.
5. **Direct card pay = "fund-and-pay", not a second rail.** Customers can
   pay per-purchase by card; internally it is card charge (webhook-confirmed,
   like the existing top-up flow) → instant wallet credit → immediate wallet
   debit → purchase. One pipeline/state machine; failed deliveries refund to
   the wallet instantly instead of a 3–7 day gateway refund. Surface gateway
   fees (or a minimum) on direct card purchases; nudge toward wallet
   pre-funding.
6. **Catalog: full VTPass catalog, catalog-driven** (no hardcoded billers),
   with **per-category admin toggles**. Launch categories are flipped on one
   at a time after sandbox verification (airtime/data first; electricity,
   cable, education, insurance as each one's verify/receipt/recon flow is
   proven).

## Why a separate VAS wallet (verified in code, do not revisit casually)

- `app/services/collections/_core.py:_resolve_prepaid_available_balance`
  computes `credit − open invoices`: the existing wallet IS service money in
  the eyes of collections/enforcement (and the reconciler's billing-aware
  guards). Same-wallet VAS would (a) let dunning effectively garnish airtime
  money, (b) let VAS spending push paying customers toward enforcement.
- VAS wallet = same ledger machinery, distinct balance dimension
  (`wallet_kind: service | vas`). Top-up screen offers a destination;
  internal transfer between the two is allowed and instant.

## Architecture

Reuses existing primitives — point them at a new target:

| Need | Existing primitive |
|---|---|
| Catalog sync | periodic Celery sync-job pattern (legacy BSS/CRM syncs) |
| Wallet debit concurrency | `billing/_common.lock_account()` (mandatory for every VAS debit) |
| Idempotency | gateway `request_id` discipline (we mint it, VTPass echoes it) |
| Delivery truth | webhook + requery, like payment-webhook settlement (#204) |
| Reconciliation | daily compare vs provider report, like the shadow billing reconciler |
| Secret custody | `credential_crypto` for tokens/PINs at rest |
| Reseller attribution | `reseller_id` on transaction (cf. `ResellerServiceRequest`) |
| Reseller payout visibility | existing reseller revenue reporting surface |

### Data model (sketch)

- `vas_services` — synced VTPass catalog: category, serviceID, variations
  (code, name, price), required identifier fields, verify-required flag,
  `is_enabled` (admin toggle, per category AND per service).
- `vas_rate_cards` — `(category, party_type[customer|reseller|owner],
  rate_pct, effective_from)`. Owner rate rows change as VTPass tiers up;
  reseller rows change only via deliberate agreement workflow.
- `vas_wallets` / ledger entries — balance dimension per subscriber/reseller.
- `vas_transactions` — request_id (unique), party (subscriber_id /
  reseller_id), serviceID + variation, identifier (phone/meter/smartcard),
  amount, **rate snapshot** (vtpass_rate, reseller_rate, owner_net at time of
  sale), state, token/PIN (encrypted, write-ahead before `delivered`),
  provider refs, timestamps.
- `vas_float_ledger` — VTPass float balance tracking + per-category pause
  thresholds.

### Transaction state machine

```
pending → debited → submitted → delivered          (commission ledger entries emitted HERE)
                              ↘ unresolved → (requery loop, capped) → delivered | failed
          debited/submitted/unresolved → failed → auto_refunded (VAS wallet credit)
```

Rules:
- **Never trust the pay response.** `pending`/timeout/ambiguous → the requery
  endpoint is the source of truth. Requery loop with backoff + cap; terminal
  ambiguity goes to a manual-review queue, never silent.
- **Commission on confirmed delivery only** — split-ledger entries (VTPass
  gross / reseller payout / owner net) are written by the
  `→ delivered` transition and nowhere else.
- **Write-ahead token custody**: token/PIN persisted (encrypted) before the
  row is marked delivered; retrievable in-app indefinitely.
- **Float gate in the purchase path**: `float < category_threshold` →
  category disabled at quote time (no debit ever happens). Alerts are layer
  two (NB: notifications runner currently OFF — the gate cannot depend on a
  human seeing an alert).

### API (bearer, /me + /reseller)

- `GET /me/vas/catalog` (enabled categories/services/variations)
- `POST /me/vas/verify` (meter/smartcard/profile validation — show the
  resolved customer name before money moves)
- `POST /me/vas/purchases` (idempotent via client-supplied or minted request_id)
- `GET /me/vas/purchases` / `GET /me/vas/purchases/{id}` (incl. token redisplay)
- `GET/POST /me/vas/wallet` (balance, fund-from-topup, internal transfer)
- Reseller mirrors under the reseller API with float wallet + payout
  statement (fixed rate only — see Invariants).

### Mobile UX

- **Home**: Wallet card is the hero (balance, Fund, Pay bills); existing
  bill/renew card beside it ("₦X due — Pay from wallet").
- **Pay-bills hub**: catalog-driven category grid, **DotMac pinned first**.
- **Purchase flow** (trust-critical): service → identifier with
  **verify-echo** (resolved customer name) or prefix→network auto-detect +
  mismatch warning → amount/variation → confirm screen (echo + fee) →
  **transaction auth via existing local_auth biometric/PIN** above a
  threshold → honest processing state (electricity can take a minute) →
  receipt with token huge/copyable, persisted to history.
- **History**: status chips, token re-retrieval, "buy again" (doubles as
  duplicate-intent guard surface).
- **Failure UX is the brand moment**: instant "₦X is back in your wallet" —
  refunds visible in history, never buried.
- **Reseller app**: float card, sell flow = purchase flow + customer phone
  field, commission statement (fixed rate only — override never renders).
- **Web portal**: wallet + DotMac-bill-pay at parity in Phase 1; full
  bill-pay hub follows mobile.

## Go decision & phasing (2026-06-12)

**GO, sequenced after core-launch stabilization.** Rationale: wallet
inversion changes how customers pay — do not land it in the launch window;
notifications runner (currently OFF) is a soft dependency for receipts/
refund comms.

- **Phase 0 (now, owner, parallel with launch)**: VTPass business account +
  sandbox keys + rate card; reseller fixed rates; gateway subaccount;
  PSP/regulatory question.
- **Phase 1**: wallet core — ledger dimension, holds, segregated settlement,
  DotMac-as-biller payment, auto-deduct toggle. Feature-flagged, web+mobile.
  (Valuable even if VTPass commercials disappoint — improves collections.)
- **Phase 2**: VTPass adapter, catalog sync, purchase state machine +
  requery + recon, airtime/data live.
- **Phase 3**: electricity/cable/education toggles; reseller float +
  commission engine.

## Invariants (enforced by tests)

1. No VAS code path reads or writes the service credit balance.
2. Commission entries exist ⇔ transaction is `delivered`.
3. Refund ⇔ terminal `failed`; a transaction can never be both delivered and
   refunded (state machine + DB constraint).
4. Reseller-facing serializers contain no owner-rate/override/net fields
   (build-failing arch-style test, RBAC-overhaul lesson).
5. Every VAS debit holds `lock_account` (or the VAS-wallet equivalent).
6. Token present before `delivered` for token-bearing categories.

## Edge cases (shape the schema now)

1. **Late delivery after auto-refund** (requery timeout → refund → delivered
   webhook arrives): terminal states are monotonic; late confirmations land
   in a manual-review queue with auto re-debit only if balance covers, else
   a receivable flag. Per-category auto-refund timers — electricity only
   refunds on definitive `failed`, never on timeout.
2. **Funds holds**: available = balance − in-flight holds (`debited`/
   `unresolved` purchases hold funds). Auto-deduct sweep and concurrent
   devices respect holds; `lock_account` alone is not enough.
3. **Double settlement on the DotMac bill** (wallet sweep + card autopay +
   manual): invoice settlement capped at balance_due, idempotent — reuse the
   #204 webhook-settlement discipline.
4. **Kobo rounding**: each split rounds down, remainder to owner;
   `Σ splits == gross` enforced as an invariant.
5. **Card-fraud cash-out** (stolen card → wallet → airtime): 3DS always,
   velocity limits (₦/day funding, purchases/hr, new recipients/day),
   maturation rule for fresh funds, fund-and-pay per-txn cap below the
   wallet-balance cap. Chargebacks debit the segregated account — track as
   a fraud-loss ledger category.
6. **Wrong-identifier delivery is unrecoverable** (esp. airtime — no verify
   endpoint): prefix→network auto-detect with mismatch warning, recent
   recipients, explicit confirm echo.
7. **Catalog/price drift**: re-validate variation + price server-side at
   purchase; reject stale prices ("refresh"), never honor them.
8. **Per-service circuit breaker** (one DisCo down ≠ category down):
   error-rate spike auto-pauses the service; DisCo nightly maintenance
   windows surfaced in UI. Same breaker pattern as Zabbix reachability.
9. **Duplicate intent** (new request_id from a re-tap): soft guard — same
   service+identifier+amount within N minutes prompts "buy again?".
10. **Subscriber merge / churn semantics**: wallet follows subscriber merges
    (summed, ledger-traced — cf. the 4,499 CRM duplicate merge); churned
    accounts keep balances as held liabilities with a manual refund path.

## Refunds (vs the no-withdrawal line)

Principle: **refund = exception path, returns money to its source;
withdrawal = feature, sends money anywhere.** The first is a merchant
obligation (Nigerian consumer protection requires refunds for services not
rendered); only the second is e-money. Three scenarios:

1. **Failed VTU delivery** → automatic instant wallet credit (core state
   machine; no human).
2. **Wallet balance out**: support-initiated request (button opens a ticket,
   never moves money). Executed as a **gateway refund against the original
   top-up transaction** (refund-to-source) — this is simultaneously the
   fraud control (stolen-card funds return to the cardholder), the AML
   answer, and the regulatory distinction from redeemability. Fallback for
   expired refund windows: manual bank transfer to a **subscriber-name-
   matched** account, admin-approved, audited. SLA required — the refund
   path must be easier than a chargeback, because the dispute button always
   exists.
3. **DotMac service not rendered**: existing billing credit-note/refund
   machinery; wallet-paid bills credit back to the wallet (or scenario 2).
   Refund ledger entries reduce wallet liability symmetrically.

## Risk knobs — recommended starting values (2026-06-12, tune after 30 days)

Structural advantage: wallet users are KYC-anchored ISP subscribers (name,
billing history, verified install address) — friendlier limits are justified
vs a standalone VTU app. All knobs live in a settings domain
(`settings_spec` pattern), never constants; velocity counted per account AND
funding card AND device; soft-declines logged (tuning dataset + alarm).

- Transaction auth (existing local_auth biometric): **≥ ₦5,000** per
  purchase; ALWAYS for new device, new recipient > ₦2,000, auto-deduct
  toggle, refund requests; re-auth after 5 min backgrounded mid-purchase.
- Funding: ₦50k/txn, ₦100k/day (3DS always).
- Purchases: ₦50k/txn from balance; **₦20k/txn fund-and-pay**;
  **airtime ₦20k/day** (the cash-out vector); 5 new airtime recipients/day;
  fresh-funds maturation — top-ups < 1h old spendable up to ₦20k.
- Per-account override tier from day one; the override flow upsells
  "become a reseller" (converts limit friction into reseller acquisition).

## Commercial inputs to fill before build (owner)

- VTPass tier ladder per category + our negotiated/starting rates.
- Reseller fixed rates per category (the locked agreement numbers).
- Breakeven table: monthly ₦ volume per category to reach next tier vs
  current customer-direct volume projection.
- Float sizing per category (electricity likely dominates ₦ volume at <1%
  margin and highest support load — decide if it launches in wave 1).
- Regulatory check: customer float/wallet volumes vs CBN/PSP thresholds —
  **weight this higher now**: a wallet that pays third-party billers is
  closer to stored-value/e-money territory than a pure service prepayment;
  confirm it rides under the aggregator's licensing at projected volumes.

## Open (not blocking design)

- Showmax/Smile/Spectranet & education PIN inventory semantics per category
  toggle review.
- Whether reseller VAS float funding ties into existing reseller billing
  (bank-transfer proofs flow) or is VAS-wallet-only.
