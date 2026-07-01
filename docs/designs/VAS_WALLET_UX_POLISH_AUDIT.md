# VAS / wallet / bill-payments (VTU) — UX-polish & operator-control audit

**Date:** 2026-06-29
**Method:** single-agent read-only review of VAS wallet + purchases (admin + customer
+ mobile), VTPass path.
**Status:** implementation update applied 2026-07-01 on branch
`codex/vas-wallet-ux-polish-audit`. Required P0/P1/P2 items are addressed; remaining
items are recommended follow-ups.

## What this audit is

Two tracks (definition in `NETWORKING_UX_POLISH_AUDIT.md`): **POLISH** and
**CONTROL**. The engine is well-built (settings-backed limits, terminal-state locks,
row-locked refund/deliver races, idempotency + recent-debit double-submit guards,
float gate, refund-on-failure) — gaps are at the edges (error surfacing, one missing
confirm, provider/dedupe hardcodes).

## Acceptance criteria (VAS-specific)

1. Customer-facing money errors show a readable message, never a raw error dict.
2. A legitimate repeat purchase has a path (confirm-and-retry), not a dead-end.
3. Refund-to-source works for the provider that funded the wallet, not just one.
4. Limits/dedupe windows are single-sourced (displayed == enforced) and tunable.

## Cross-cutting themes

### POLISH

**P-A. Error / result surfacing.**
- Web purchase error handler does `error={exc.detail}` but `debit_wallet` raises
  `detail` as a `{code,message}` dict → customer sees a raw Python dict repr; the
  sibling pay-bill route already unwraps `.get("message")` (`app/web/customer/bills.py:135-138`)
- Duplicate-intent guard returns 409 "confirm to buy again", and the service +
  mobile API support `confirm_duplicate`, but the web form never passes it → a
  legitimate repeat is a dead-end with a misleading message (`bills.py:126`)
- Receipt `processing`/submitted state says "Refresh for the latest status" with no
  auto-poll; a delivered/refunded outcome (5-min requery sweep) is invisible until
  manual reload (`templates/customer/bills/receipt.html:24`)
- `run_auto_deduct_sweep` returns paid/errors counts but the admin VAS page surfaces
  no partial-success/failed-sweep result (`vas_wallet.py:601-615`)

**P-B. Confirm on irreversible admin money action.** Admin "Mark delivered" (review
queue) is terminal and settles reseller commission with no reverse path, but has no
`confirm()` while the adjacent Refund actions do (`templates/admin/system/vas.html:67-70`).

### CONTROL

**C-1. Provider hardcode (correctness).** Refund-to-source hardcodes
`paystack.refund_transaction(...)`, ignoring `default_payment_provider` that
`_provider()` honors elsewhere → a wallet funded via a non-Paystack provider can't
be refunded to source (`app/web/admin/vas.py:233-236`).

**C-2. Duplicated / inconsistent constants (drift).**
- Two hardcoded double-submit windows: purchase dedupe 5 min vs pay_bill 60 s,
  neither tunable (`vas_purchases.py:522` vs `vas_wallet.py:461`)
- Top-up limit defaults duplicated — enforced in `_initiate_topup_for_wallet` and
  re-declared for display in `wallet_overview`; can silently drift (`vas_wallet.py:246-248,554-555`)

**C-3. Operator/biller-specific policy hardcoded.**
`SLOW_SETTLEMENT_CATEGORIES={"electricity-bill"}` + `REQUERY_MAX_ATTEMPTS=10`
(`vas_purchases.py:47,49`); VTPass HTTP timeouts 20s/45s (`vtpass.py:69,83,131,186`);
currency `"NGN"` + `₦` literals (`vas_wallet.py:274,553`).

## Priority

| Tier | Status | Items |
|------|--------|-------|
| **P0** | **Done** | Refund-to-source now resolves the funding provider and routes through the payment gateway adapter instead of hardcoded Paystack. Customer web purchase errors now unwrap structured `{code,message}` details into readable messages. |
| **P1** | **Done** | Web purchases now pass `confirm_duplicate`; the form includes a repeat-purchase confirmation checkbox. Admin Mark Delivered has a terminal-action confirm. Processing receipts auto-refresh. Top-up limits and purchase/pay-bill dedupe windows are single-sourced through VAS settings. |
| **P2** | **Done** | Requery cap, slow-settlement categories, and VTPass timeouts are settings-backed. Auto-deduct sweep results are recorded and surfaced on the admin VAS page, with a manual run action. Wallet/bills/admin VAS currency display now reads from configured currency helpers. |

## Implementation update — 2026-07-01

### Done

- [x] **P0 / required:** customer bill-purchase errors unwrap structured
  `HTTPException.detail` dictionaries and redirect with a readable message.
- [x] **P0 / required:** refund-to-source uses
  `payment_gateway_adapter.refund(...)`; Paystack and Flutterwave are supported.
  The funding provider is read from the top-up memo written at verification,
  with the current default provider as a legacy fallback.
- [x] **P1 / required:** duplicate web purchases can be explicitly confirmed via
  a form checkbox and `confirm_duplicate` reaches the purchase service.
- [x] **P1 / required:** admin review "Mark delivered" has a confirm because it
  is terminal and settles commission.
- [x] **P1 / required:** non-terminal customer receipts auto-refresh every 20s.
- [x] **P1 / required:** top-up limits are exposed through one `topup_limits`
  helper used by enforcement and display.
- [x] **P1 / required:** purchase and wallet bill-payment dedupe windows are VAS
  settings with defaults matching previous behavior.
- [x] **P2 / required:** VAS requery cap, slow-settlement category list, and
  VTPass GET/POST/verify/requery timeouts are settings-backed.
- [x] **P2 / required:** auto-deduct sweep records its last result and the admin
  VAS page shows paid/error/total counts; operators can run the sweep manually.
- [x] **P2 / required:** wallet and bill-payment currency display uses configured
  currency code/symbol helpers instead of hardcoded `NGN`/`₦` in the audited
  screens.

### Still left

- [ ] **Recommended:** store the funding provider as a structured wallet-entry
  field in a future migration. The current implementation uses the memo written
  for new top-ups and a default-provider fallback for legacy rows.
- [ ] **Recommended:** add first-class Flutterwave checkout UI for wallet top-up
  if operators enable Flutterwave as the default provider for customer portal
  wallet funding. Refund-to-source support is now provider-aware.
- [ ] **Recommended:** replace query-string sweep status with a richer run-history
  table if operators need audit-grade sweep observability.
- [ ] **Recommended:** broaden currency cleanup outside the audited VAS/wallet
  screens as part of a full multi-currency pass.

## Appendix — full findings
- [POLISH] (High) `app/web/customer/bills.py:135-138` — `error={exc.detail}` but detail is a `{code,message}` dict → **DONE:** readable message unwrap.
- [POLISH] (Med) `app/web/customer/bills.py:126` + `bills/index.html` — duplicate-intent 409 confirm path supported by service/mobile but web never sends `confirm_duplicate` → **DONE:** confirm checkbox + route parameter.
- [POLISH] (Med) `templates/admin/system/vas.html:67-70` — admin "Mark delivered" terminal + settles commission, no confirm (refunds confirm) → **DONE:** confirm added.
- [CONTROL] (Med) `app/web/admin/vas.py:233-236` — refund-to-source hardcodes paystack, ignores `default_payment_provider` → **DONE:** provider-aware adapter refund.
- [POLISH] (Med) `templates/customer/bills/receipt.html:24` — processing state "Refresh for status", no auto-poll → **DONE:** auto-refresh while non-terminal.
- [CONTROL] (Med) `vas_purchases.py:522` vs `vas_wallet.py:461` — purchase dedupe 5min vs pay_bill 60s, neither tunable → **DONE:** VAS settings for both windows.
- [CONTROL] (Med) `vas_purchases.py:47,49` — `SLOW_SETTLEMENT_CATEGORIES`/`REQUERY_MAX_ATTEMPTS` hardcoded → **DONE:** VAS settings for category list and bounded cap.
- [CONTROL] (Med) `vas_wallet.py:246-248,554-555` — top-up limit defaults duplicated (enforced vs displayed) → **DONE:** centralized `topup_limits`.
- [CONTROL] (Low) `vas_wallet.py:274,553` + templates — currency `"NGN"`/`₦` hardcoded → **DONE:** audited wallet/bill-payment screens use configured currency helpers.
- [CONTROL] (Low) `vtpass.py:69,83,131,186` — provider timeouts 20s/45s hardcoded, no retry/backoff policy → **DONE:** VTPass timeout settings.
- [POLISH] (Low) `vas_wallet.py:601-615` — auto-deduct sweep failures not surfaced on admin VAS page → **DONE:** last-run and manual-run result surfacing.
- Verified: pay_bill idempotency (reserve-before-money + replay), top-up verify idempotency on reference, float gate, terminal-state locks, bills purchase JS submit-guard — solid.
