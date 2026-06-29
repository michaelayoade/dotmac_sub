# VAS / wallet / bill-payments (VTU) — UX-polish & operator-control audit

**Date:** 2026-06-29
**Method:** single-agent read-only review of VAS wallet + purchases (admin + customer
+ mobile), VTPass path.
**Status:** audit only. Part of the remaining-module audit series. (The reseller VAS
portal page is covered in `RESELLER_UX_POLISH_AUDIT.md`.)

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

| Tier | Items |
|------|-------|
| **P0** | Refund-to-source hardcoded Paystack → non-Paystack wallet unrefundable (C-1); web purchase shows raw error dict to customer (P-A) |
| **P1** | Wire duplicate-confirm path on web (P-A); admin Mark-delivered confirm (P-B); receipt auto-poll (P-A); centralize top-up-limit defaults + dedupe windows as settings (C-2) |
| **P2** | slow-settle categories / requery cap / VTPass timeouts as settings (C-3); auto-deduct sweep result on admin page; currency-from-setting if multi-currency |

## Appendix — full findings
- [POLISH] (High) `app/web/customer/bills.py:135-138` — `error={exc.detail}` but detail is a `{code,message}` dict → raw dict shown; sibling unwraps `.get("message")` → mirror unwrap [recommend]
- [POLISH] (Med) `app/web/customer/bills.py:126` + `bills/index.html` — duplicate-intent 409 confirm path supported by service/mobile but web never sends `confirm_duplicate` → dead-end → wire confirm checkbox/resubmit [recommend]
- [POLISH] (Med) `templates/admin/system/vas.html:67-70` — admin "Mark delivered" terminal + settles commission, no confirm (refunds confirm) → add confirm [recommend]
- [CONTROL] (Med) `app/web/admin/vas.py:233-236` — refund-to-source hardcodes paystack, ignores `default_payment_provider` → non-Paystack top-up unrefundable → route via funding provider/adapter [recommend]
- [POLISH] (Med) `templates/customer/bills/receipt.html:24` — processing state "Refresh for status", no auto-poll → poll receipt/mobile detail while non-terminal [recommend]
- [CONTROL] (Med) `vas_purchases.py:522` vs `vas_wallet.py:461` — purchase dedupe 5min vs pay_bill 60s, neither tunable → vas settings (dedupe_window_seconds) [defer]
- [CONTROL] (Med) `vas_purchases.py:47,49` — `SLOW_SETTLEMENT_CATEGORIES`/`REQUERY_MAX_ATTEMPTS` hardcoded → vas settings (category list + bounded cap) [defer]
- [CONTROL] (Med) `vas_wallet.py:246-248,554-555` — top-up limit defaults duplicated (enforced vs displayed) → centralize in one helper [recommend]
- [CONTROL] (Low) `vas_wallet.py:274,553` + templates — currency `"NGN"`/`₦` hardcoded → source from billing settings if multi-currency [defer]
- [CONTROL] (Low) `vtpass.py:69,83,131,186` — provider timeouts 20s/45s hardcoded, no retry/backoff policy → vas/provider settings if tuning needed [defer]
- [POLISH] (Low) `vas_wallet.py:601-615` — auto-deduct sweep failures not surfaced on admin VAS page → show last-run result/error count [defer]
- Verified: pay_bill idempotency (reserve-before-money + replay), top-up verify idempotency on reference, float gate, terminal-state locks, bills purchase JS submit-guard — solid.
