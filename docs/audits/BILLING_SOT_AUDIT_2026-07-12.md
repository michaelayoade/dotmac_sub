# Billing domain — source-of-truth audit

**Date:** 2026-07-12
**Scope:** `financial_access` domain of `docs/SOT_RELATIONSHIP_MAP.md`, plus the billing
projections into `dotmac_crm` and `dotmac_erp`.
**Method:** six parallel read-only audits (money movement, invoice lifecycle, payment
ingress, billing→access consequences, adapter-layer compliance, cross-repo projections).
No code was changed. Every finding below carries a `file:line`.

---

## 1. Ownership model: federated, bounded sources of truth

This audit does **not** claim a single universal financial source of truth. Sub, ERP and CRM
are bounded contexts with distinct, non-competing authority:

- **Sub** owns billing facts and operational customer financial state.
- **ERP** owns accounting classifications and journals.
- **CRM** owns its explicitly assigned CRM state.
- **Synced copies are projections, never alternative authorities.**
- **Reconciliation harmonizes systems without transferring ownership.**

A finding below is a violation when a system acts as an authority outside its bounds, when a
projection overwrites a field it does not own, or when *within* a bounded context a decision
or derived field has no single named owner. It is not a violation for ERP to represent Sub's
facts differently in its own books — that is ERP's bounded authority.

## 2. Verdict

Within Sub's own bounded context, the declared source of truth is not the operative source of
truth.

`financial.ledger` is named in `app/services/sot_relationships.py:77` as the owner of
"posted money movement" and "ledger-derived balances". In practice:

- `LedgerEntries` (`app/services/billing/ledger.py`) is imported in exactly one place
  (`app/services/billing/__init__.py:36`) and reached only from REST routes. **Every one of
  the 16+ real posting sites in the app constructs `LedgerEntry(...)` by hand.**
- The real balance and locking primitives live in `app/services/billing/_common.py:51`
  (`get_account_credit_balance`) and `:99` (`lock_account`) — not in the owner module.
- Because the owner **does not mediate domain-generated postings — it is used only by generic
  ledger CRUD** — it can enforce no invariant. Every violation in this report walks through
  that loophole.

Consequently **"balance" has four non-equivalent definitions**, and the one that decides
suspension is not the ledger:

| # | Definition | Where | Who trusts it |
|---|---|---|---|
| 1 | Ledger-derived: `SUM(credit) − SUM(debit)` over unallocated, active entries | `billing/_common.py:51` | settlement, `reconcile_unposted`, `settle_from_credit` |
| 2 | Document-derived: re-aggregates Payments + Allocations + Invoices + CreditNotes + **the decommissioned Splynx mirror** + a filtered subset of ledger rows | `customer_financial_ledger.py:254` | **enforcement/suspension**, customer portal, CRM push |
| 3 | Admin open-balance: its own `SUM(Invoice.balance_due)` subquery | `web_billing_accounts.py:55` | admin account list |
| 4 | VAS wallet: a second append-only ledger, **absent from the SOT registry entirely** | `vas_wallet.py:136` | wallet, `pay_bill` |

Definitions 1 and 2 are **both authoritative for different decisions**. The customer sees
(2) and is suspended on (2); the money-moving code spends against (1). They drift by
construction — see F4.

**The repair layer is load-bearing, not corrective.** `billing_prepaid_overlap_repair.py`
runs *inline in the hot dunning loop* (`collections/_core.py:1456`), and
`_restore_wrongly_suspended_subscriptions` (`:384`) exists to un-suspend customers the
system wrongly suspended. These are not reconcilers repairing drift from an authoritative
input; they are patches for an unsound decision path. Per the standard, that is the signal
that the owner is wrong.

**Why the logic ended up here.** `tests/architecture/test_thin_wrappers.py` forbids direct
queries in `app/web` and `app/api`, and `test_thin_financial_tasks.py` forbids model imports
in four Celery files. Both hold — the routes and tasks are genuinely clean. But **nothing
polices the ~30 `web_billing_*.py` modules between routes and owners** (11,900 lines, versus
13,037 in the owners themselves). Displaced business logic landed exactly where the guard
wasn't looking.

---

## 3. Critical — money correctness

> **Impact language.** Findings state **possible** impact derived from static analysis unless
> explicitly marked *confirmed*. A **confirmed** finding has a recorded production incident or a
> drift detector that currently fires on real data.
>
> **A read-only reconciliation pass has since been run against staging
> (`sha-81b7c35`) — see `BILLING_ALIGNMENT_RUN_2026-07-12.md`.** Measured incidence:
>
> | Finding | Measured | Meaning |
> |---|---|---|
> | F1 double-swing | **0 occurrences** | **Latent.** Proven defective in code (`tests/test_ledger_reversal_integrity.py`), never fired. Fix forward; **no historical repair.** |
> | F3 misallocation | **2 payments, ₦60,000** | **Fired in production.** |
> | F24 paid-with-balance | 23 invoices, ₦411,821 | Fires today. |
> | F19 orphan payment | **1 native payment** | Real but tiny (3,116 "orphans" were splynx-imported by design). |
> | F15 NULL `paid_at` | 1 payment | The earlier fix largely held. |
> | F4 unapplied credit notes | 339, ₦2,290,830 | The drift mechanism is populated. |
> | F18, F6-void, opening debits | **0** | Zero-result. Opening-debit cohort already remediated. |
>
> Do not infer scope from severity: the highest-severity code defect (F1) has **zero** historical
> damage, while a medium-looking one (F3) actually fired.

### F1 — `LedgerEntries.reverse()` swings the balance by twice the reversed amount

`app/services/billing/ledger.py:112-149` posts a reversing entry **and** sets
`original.is_active = False`. Both balance readers filter on active rows
(`_common.py:78`, `:90`).

Reversing an unallocated ₦10,000 credit therefore: removes the +10,000 (original goes
inactive) **and** subtracts a new 10,000 debit → the balance moves by −20,000 instead of
−10,000. The customer's available balance goes wrongly negative; enforcement keys on that
balance → **wrongful suspension of a paying customer**.

Reachable in production: `POST /api/billing/ledger-entries/{id}/reverse`
(`app/api/billing.py:1463`). The same deactivate-and-reverse shape recurs in the invoice
void path (`billing/invoices.py:536-547`).

The module docstring declares entries immutable, yet the class also exposes `update()`
(`:56`, arbitrary `setattr` including `amount` — callable but currently unrouted) and
`delete()` (`:152`, soft-deactivation with **no compensating entry**, and routed at
`api/billing.py:1474`) — a silent balance mutation with no audit trail.

**Fix:** the ledger is append-only. `reverse()` must post the reversing entry and leave the
original active. `delete()` and `update()` must go. Decide `is_active` semantics once —
either it is an exclusion filter (then never post a reversal) or a reversal marker (then
never deactivate); today the two halves disagree.

### F2 — Dead-letter replay discards the money and marks itself resolved

`app/services/api_billing_webhooks.py:539-567`. The live ingest path calls
`_extract_settlement` and passes `amount`, `currency`, `invoice_id`, `account_id`,
`status_hint` (`:311-318`). The **replay** path passes none of them (`:542-551`).

Inside `providers.py:259` the guard is `if not payment and payload.amount and (...)` →
`payload.amount` is `None` → no Payment is created → the event is marked `failed`
internally without raising. Back in the replay, `_resolve_dead_letter(..., replayed)` fires
unconditionally at `:567`.

And `billing_enforcement_guards.py:253-262` counts dead letters only in
`received|failed|rejected` — **`replayed` is not counted**.

So the exact event dead-lettering exists to rescue (a `charge.success` whose ingest crashed)
is replayed, the money is never posted, the invoice stays open, the customer stays
suspended — and the row leaves both the ops queue *and* the health gate. Missed settlement
with the alarm switched off.

### F3 — Admin payment re-allocation corrupts AR

`app/services/web_billing_payments.py:379-406`, wired into the admin "edit payment" flow at
`:1226-1234` (immediately after the owner's `payments.update`).

```python
for alloc in list(payment_obj.allocations):
    db.delete(alloc)                      # hard delete, not soft
db.add(PaymentAllocation(
    payment_id=payment_obj.id,
    invoice_id=UUID(requested_invoice_id),
    amount=payment_obj.amount,            # always the FULL payment, uncapped
))
```

Versus the owner (`billing/payments.py:436` + `:352` + `_recalculate_invoice_totals`), this
skips: the ledger entry entirely; the recalculation of **either** invoice; any cap against
the target's `balance_due`; and the account-status/service restore. The old invoice keeps
`status=paid, balance_due=0` with no money behind it; the ledger still credits the payment
to the old invoice while the allocation points at the new one — a double-count.

### F4 — Credit notes visible to one balance, invisible to the other

`catalog/subscriptions.py:1037` and `billing_automation.py:2301` create issued `CreditNote`
rows with **no ledger entry** (only `CreditNotes.apply()` posts one, `credit_notes.py:278`).

An issued credit note is money the customer holds. It raises balance definition (2) — the
portal and enforcement — and is invisible to definition (1). The customer can then spend it
on an add-on (`customer_portal_flow_addons.py:293`), which posts a ledger **debit** — driving
the ledger balance negative on money the ledger never saw arrive.

This is a clean, mechanical path to wrongful suspension of a customer who genuinely holds
credit, and it is the mechanism behind the phantom-debit incidents already in the memory
record.

### F5 — ERP's Sub projection does not produce a complete accounting posting

Dotmac Sub remains authoritative for invoice and payment events. ERP remains authoritative for
how those events are represented in its accounting books. ERP GL-posts synchronized payments
but does not GL-post the corresponding synchronized invoices. Consequently, ERP's own AR
control and revenue balances can diverge from the authoritative billing events received from
Sub.

Evidence: `dotmac_erp/app/services/dotmac_sub/sync/_invoices.py:65` — *"Sync dotmac_sub
invoices → ERP AR subledger (not GL-posted)."* Payments **are** GL-posted (`_payments.py:497`
`ensure_gl_posted`); within the whole sub-sync package that is the only such call. ERP's one
automatic GL-poster requires `Invoice.status == APPROVED`
(`dotmac_erp/app/tasks/data_health.py:308`), but the mirror's status mapper only ever emits
`VOID`/`PAID`/`PARTIALLY_PAID`/`POSTED` (`sync/_base.py:402-415`) — never `APPROVED`. Net GL
effect per synced payment: `Dr Bank / Cr AR-control` with no offsetting
`Dr AR-control / Cr Revenue`.

**Fix** the ERP projection pipeline so eligible synchronized invoices are idempotently posted
before dependent payments. Repair existing ERP journals through reconciliation, backfill, and
reversal/adjustment journals. **Do not make ERP authoritative for Sub invoice, payment or
customer-access state.**

---

### F25 — VAS wallet: committed wallet debit can be lost (Critical)

Zero occurrences of "vas"/"wallet" in `sot_relationships.py` — an entire customer-liability
money system sits outside the ownership map.

`pay_bill` (`vas_wallet.py:537-575`) **commits** the wallet debit (`_write_entry` commits at
`:182`), *then* calls `Payments.create`. A process death between the two destroys the customer's
money with no recovery: the compensating `credit_wallet` at `:565` only covers a raised
exception, not a crash. Nothing reconciles the wallet store against billing, so a stranded debit
is invisible to every billing repair service.

Per the ownership decision (§10), VAS owns its wallet — but the **transfer** into billing must be
atomic or outbox-backed.

## 4. Critical — customer-visible access

### F6 — Four payment paths restore service with no balance gate, and `payment` can clear a *prepaid* lock

`account_lifecycle.py:67`: `EnforcementReason.prepaid: {"top_up", "payment", "admin"}`.

`collections/_core.py:1935-1949` `_restore_account` defaults to `trigger="payment"` with no
`reason=`, so `resolve_locks_for_trigger` clears **every** overdue *and* prepaid lock.

These four callers apply **no gate at all**, and sit outside the `if settled.changed:` block
so they fire even when the top-up was underfunded and the draft was left unsettled:

- `customer_portal_flow_payments.py:430` and `:1812`
- `api_billing_webhooks.py:447`
- `payment_reconciliation.py:258`

The event handler that *does* gate on `has_overdue_balance` (`events/handlers/enforcement.py:603`)
is bypassed — the inline call already restored before the event fires.

**Result — free service.** A prepaid customer suspended below a ₦5,000 `min_balance`, or a
postpaid customer owing ₦50,000, tops up ₦100 → restored. The prepaid sweep then re-suspends
on its next run → flapping.

Root cause is a category error (F7): the suspend criterion is a *balance threshold*, but the
restore criterion is `has_overdue_balance` — an **invoice** question that is structurally
False for prepaid accounts (prepaid rows are excluded from dunning at `_core.py:1471` and
held at `:145`). The gate can never fail for the cohort it guards.

### F7 — `prepaid_balance_sweep` suspends with no shield and no health gate

Its own comment (`prepaid_balance_sweep.py:341-346`) claims `_suspend_account` fails closed
for *shielded* accounts. It does not: `_suspend_account` (`collections/_core.py:366-441`)
checks only `status == canceled` (`:389`) and `_account_has_dedicated_bundle` (`:393`).
Grep for `shield` or `billing_enforcement_health` in `prepaid_balance_sweep.py`: **zero hits.**

Dunning re-checks all of it before enforcing (`collections/_core.py:975-1027`): billing-profile
validity, live balance, shield (active payment arrangement / submitted bank-transfer proof /
service extension), and the enforcement health gate.

**Result — wrongly suspended payer.** A prepaid customer with an approved payment arrangement,
or a bank-transfer proof under review, is cut off by the sweep. The dunning path would have
protected them.

### F8 — Five restore criteria, three incompatible thresholds

| Path | Criterion |
|---|---|
| Payment event, invoice paid, void/write-off | `not has_overdue_balance` |
| Top-up / webhook / reconcile (4 sites) | **none** |
| `prepaid_balance_sweep` | `balance >= min_balance` |
| `stale_overdue_lock_reconcile` | `available >= _minimum_required_balance` |
| `unwall_paid_accounts` | `available >= 0` |

`unwall_paid_accounts` (`>= 0`) will restore precisely the accounts `prepaid_balance_sweep`
(`>= min_balance`) is suspending. It is CLI-only today — **that is the only thing preventing
a permanent oscillation. Do not schedule it** until the thresholds are unified.

---

## 5. High — the declared control plane does not run

### F9 — `access.radius_state` is statically unreachable under the registered configuration

The registry (`sot_relationships.py:726-778`) declares the chain
`access.control_resolution` → `access.radius_state` → `access.session_enforcement`.

(Static analysis only. Confirming it has never run in production requires telemetry; the
claim here is that no registered configuration can reach it.)

`radius_access_state.set_subscription_access_state` has exactly one caller
(`events/handlers/enforcement.py:90`), gated on
`enforcement_event_policy.group_routing_enabled(db)`, which defaults **False** and has
**zero entries in `settings_spec.py`** → `resolve_value` returns `None` → falsy → always off.

This is the *same failure mode* as the `prepaid_monthly_invoicing_enabled` bug fixed in #396:
**a flag with no `SettingSpec` is permanently off and cannot be enabled.** It has now recurred.

`access.control_resolution` is likewise not a decision owner — it has four read-side callers
and **zero** billing/dunning/prepaid/admin/payment callers.

Meanwhile **four writers** actually reach the external RADIUS DB: `radius_population.populate()`
(the resolver-based one, self-declared sole writer), `radius.py::_external_sync_users:1165`
(its own divergent status→radcheck rules — `blocked` gets no rows; ignores `subscriber.status`
entirely, so an account-level-suspended subscriber with a stale-active subscription is rebuilt
**with a working password**), `enforcement.py::_delete_users_from_external_radius:1623`, and the
dead flag-gated path.

### F10 — Whole-subscriber RADIUS wipe on single-subscription suspend

`events/handlers/enforcement.py:157-163` documents that these calls were removed because they
"acted on the WHOLE SUBSCRIBER — suspending one subscription wiped auth for the subscriber's
other active logins". The admin/catalog path still reaches an identical deleter:
`catalog/subscriptions.py:520-525` → `enforcement.py:1497-1505` (queries by
`subscriber_id`, not subscription) → raw `DELETE` on radcheck/radreply/radusergroup.

**Result:** suspending one service kills auth for the customer's other, paid, active services
until the next `populate()` sweep (up to 15 minutes).

---

## 6. High — invoice lifecycle has no registered owner

The registry names no invoice-lifecycle owner. `Invoices` + `_common.py` are the *de facto*
owner and roughly eight writers bypass them.

### F11 — `BillingAdapter`'s DTOs drop the fields every guard keys on

`billing_adapter.py:26-43`: `InvoiceIntent` has no `billing_period_start`/`_end`;
`InvoiceLineIntent` has no `subscription_id` (though `InvoiceLineBase.subscription_id` exists,
`schemas/billing.py:87`).

So `web_catalog_subscriptions.py:2913` mints an `issued` invoice with NULL period and NULL
subscription. Three consequences:

1. **Double-bill.** The runner's idempotency check (`billing_automation.py:1320-1330`) filters
   on exactly those two fields → can never match → the first period is billed again.
2. **Phantom AR on a prepaid account.** This path has no billing-mode check, unlike the owner's
   `create_for_subscription` (`invoices.py:163-171`).
3. **It can never be reclassified.** `invoice_classification.py:53-58` joins on
   `InvoiceLine.subscription_id` → NULL never enters `prepaid_non_ar_invoice_ids()` → stays
   collectible AR → marked overdue → dunning → suspension of a prepaid customer.

That is precisely the incident class the repair services already exist to clean up.

**Generalised lesson (cross-cutting):** a boundary DTO that omits the fields downstream
idempotency/classification guards key on silently disables those guards for every caller of
the façade.

### F12 — `issued → draft` is illegal in the owner and done freely by three writers

`ALLOWED_INVOICE_TRANSITIONS[issued]` (`_common.py:231-237`) excludes `draft`; the owner
returns 409. Three writers do it anyway: `billing_automation.py:1562`,
`billing/reconcile_unposted.py:481`, `billing_cleanup_remediation.py:982`. An invoice already
emitted as `invoice_sent` / exported to ERP silently reverts to draft — draft is excluded from
AR, so recognised revenue vanishes with no ledger record.

### F13 — Two overdue authorities, one of which emits no event

`billing_automation.py:2093` (`mark_overdue_invoices`) has the reconciliation-hold skip, the
prepaid-non-AR skip, grace escalation, an `overdue_event_sent` idempotency flag, and emits
`invoice_overdue`. Dunning (`collections/_core.py:1463-1467`) sets `invoice.status = overdue`
directly, emits **no event**, sets **no flag**, and uses a different prepaid guard. Enforcement
proceeds while the notification and downstream handlers never fire.

### F14 — Anti-double-bill guard protects one path in six

`billing_line_key` is the only DB-enforced line guard (unique index,
`models/billing.py:480-483`). It is set by exactly two writers (`billing_automation.py:592`,
`:1484`). Proration, usage, `create_for_subscription`, and everything via `BillingAdapter`
leave it NULL — which the partial unique index excludes. `invoices.invoice_number` has **no
unique index at all**, while the import wizard (`web_system_import_wizard.py:468`) accepts
free-form numbers that can collide with the `DocumentSequence` series.

---

## 7. High — settlement and duplicate-payment holes

### F15 — `Payments.update` is a second, unguarded settlement writer

`billing/payments.py:1647-1648` — raw `setattr` over `PaymentUpdate` (which carries `status`
and `paid_at`). No transition guard (that lives only in `mark_status`, `:1685-1696`), no
`paid_at` stamp, no `payment_received` event — **but it does run
`_finalize_invoice_payment_effects`**, so the invoice flips to paid.

This reopens the exact production regression the team already fixed. The fix landed only in
`create` (`payments.py:1273-1280`, with a comment explaining that a NULL `paid_at` blinds the
enforcement health gate and blocks all collections suspensions). An admin flipping
pending→succeeded via `PATCH /api/billing/payments/{id}` re-creates it. `refunded → succeeded`
is forbidden in `mark_status` and permitted here.

### F16 — Reseller payment-proof verify has no lock → double-credit

`payment_proofs.py:302-310` routes to `_verify_consolidated_proof` **before** the lock. The
subscriber path locks and re-checks status (`:319-321`); `_verify_consolidated_proof`
(`:392-462`) does neither. `find_duplicate_proofs` excludes the proof itself (`:112`), and the
DB backstop is inert because `uq_payments_active_external_id` requires
`provider_id IS NOT NULL` (`models/billing.py:619-621`) while proofs set none.

Two concurrent Verify clicks → two succeeded payments → reseller balance double-credited.

**Systemic:** four writers set `external_id` without `provider_id` (payment proofs, autopay,
bulk mark-paid) and therefore get **zero** database dedupe.

### F17 — Partial refund reverts the invoice to fully unpaid

`payments.py:2192` sets `status = partially_refunded`; `_recalculate_invoice_totals` counts
only `succeeded` (`_common.py:376`). Refunding ₦500 of a ₦50,000 payment drops `paid_amount`
50,000 → 0, restores the full `balance_due`, and reverts the invoice to overdue → dunning and
suspension for a customer who paid.

### F18 — Reseller bulk payment lands `pending` but still moves money

`web_consolidated_billing.py:164-174` builds `PaymentCreate` with no `status` → falls back to
`default_payment_status` = `"pending"` (`settings_spec.py:1793-1800`). Allocation and ledger
credit do **not** check status, but `_recalculate_invoice_totals` counts only `succeeded`.
The reseller's transfer credits `BillingAccount.balance` (immediately spendable) while every
member invoice stays unpaid. Half-applied money, untested (`test_billing_consolidated_web.py:31-38`
mocks the call out).

### F19 — Import wizard bypasses the owner entirely

`web_system_import_wizard.py:488-497` constructs `Payment(...)` raw, defaulting to
`status=succeeded` with no `paid_at`, no allocation, no ledger entry, no invoice recalc.
Imported cash is an orphan row: invoices stay open, `get_account_credit_balance` sees nothing,
customers keep getting dunned. The same file (`:466-487`) takes `Invoice.balance_due` straight
from the CSV.

---

## 8. Medium — adapter layer and projections

- **F20 — duplicate invoice emails.** `web_billing_invoices.py:255-345` hand-composes an invoice
  email (hardcoded HTML, hardcoded brand colours) and sends it directly, duplicating the
  canonical `invoice_sent` notification (`events/handlers/notification.py:235-240`) — *identical
  subject line*. Single-issue → customer gets **both**. Bulk issue (`web_billing_invoice_bulk.py:105`,
  raw status write, no event) → customer gets **only** the hardcoded one, no SMS, no webhook.
  Unchecking "send notification" does not suppress the canonical email.
- **F21 — the portal contradicts itself.** `customer_portal_context.py:824-837` reimplements
  `collection_blocking_balance` (`invoice_collectibility.py:209`) with two divergences: a
  `.limit(50)` and netting against wallet credit. Result: for an account whose overdue invoices
  are covered by credit, the payment-arrangement page says *"you have no overdue balance"* while
  the plan-change page says *"you have an overdue balance — settle it first"*. Same account, same
  session, opposite answers.
- ~~**F22 — billing push resets CRM subscriber status.**~~ **RESOLVED 2026-07-12.** Commit
  `b6d1accd` ("Close the CRM write-back door", #1204) deleted `app/services/crm_billing_push.py`
  outright, removing the outbound push. The projection writer no longer exists. Struck — no
  repair needed. (The *rule* it violated still stands and is why the §11 sync contract matters:
  a projection must never overwrite a field it does not own.)
- **F23 — Splynx is still load-bearing.** `customer_financial_ledger.py:112-119` derives
  `has_legacy_mirror` purely from the presence of Splynx rows and uses it to *suppress native
  pre-cutover money* (`:287`, `:338`, `:411-417`). Truncating `splynx_billing_transactions` would
  silently repartition every migrated customer's balance and enforcement state. `app/config.py:49-55`
  asserts the opposite ("retained READ-ONLY for audit only") — that comment is false. Balance
  correctness further hinges on **free-text memo prefix matching**
  (`INTERNAL_MEMO_PREFIXES`, `:42-53`), redeclared independently in four other places: a new
  remediation batch with an unlisted memo prefix silently moves every migrated customer's balance.
- **F24 — reporting disagrees with the ledger.** `billing/reporting.py:183` recognises revenue by
  `status == paid` while `:465` sums `balance_due` — and `billing_health.paid_with_balance:191`
  exists because those disagree. AR aging (`:301-306`) applies neither `collectible_ar_invoice_filter()`
  nor `is_proforma == False`, so the dashboard shows debt collections refuses to act on. The
  "Total Balance" KPI (`:259-261`) sums `Subscriber.min_balance` — an admin-entered *enforcement
  threshold*, not money.
(F25 was reclassified **Critical** and moved to §3.)

---

## 9. What is genuinely sound — do not refactor away

- **`account_lifecycle.py` is the canonical billing-path writer** of `subscription.status`:
  row-locked, legal transitions, `EnforcementLock` create/resolve, `compute_account_status`
  re-derivation. Every *money* path goes through it. It is **not the sole writer** — direct status
  writers remain elsewhere (`web_customer_actions.py:1755`, `app/api/subscribers.py:173` →
  `subscriber.py:760` blind `setattr`, `crm_api.py:1163`), which is why `account_status_reconcile`
  is load-bearing (F7 note). The `EnforcementLock` + `ALLOWED_RESTORERS` reason-scoping model is
  the right shape — the bug (F6) is that `payment` was granted `prepaid`, not the model.
- **`Payments.create` is a real owner** with genuine double-post protection: capped allocation,
  `_apply_payment_allocation` returns 0.00 for an existing allocation, `_create_payment_ledger_entry`
  returns the existing entry rather than a second credit.
- **`_recalculate_invoice_totals`** (`_common.py:288`) is the one derived-field rule the repo truly
  enforces: row-locked, terminal-status protected, correctly reopens `paid → overdue` on reversal.
- **Dunning's pre-enforcement re-check** (`collections/_core.py:975-1027`) — row-lock, then re-read
  live balance/shield/health *at the moment of enforcement*. **This is the pattern the prepaid sweep
  and the four restore sites should copy.**
- **The four Celery task modules** (153 lines total) are exemplary thin wrappers. The routes are
  clean. `_handle_invoice_overdue` correctly refuses to enforce, deferring to dunning.
- **The `paid_at` production fix did land** and covers every caller routing through `create`. Only
  the two paths that bypass `create` (F15, F19) still have the hole.
- **CRM payment ingress** (`crm_api.record_external_payment:1696`) is the template to copy:
  idempotency key backed by a real partial unique index, IntegrityError → return existing.
- **`sync_flow_ownership`** (sub→ERP outbox) is the best pattern in the estate and is exactly the
  standard's "migrate authority explicitly": one row per flow naming the owning app, refusing to
  send flows it doesn't own.
- **`reconcile_unposted.settle_open_invoices_from_credit`** is a model reconciler: locks, spends only
  payment-backed credit, posts an offsetting debit so credit can't be double-counted.
- **The invoice PDF cache is correctly versioned** (`billing_invoice_pdf.py:900-910`) — cannot serve
  a stale invoice.

---

## 10. Ownership decision (ADR input)

Sub's billing context decomposes into **named, bounded owners** — not one universal ledger:

| Owner | Owns |
|---|---|
| `financial.customer_credit_ledger` | Spendable customer credit; append-only operational postings |
| `financial.invoice_lifecycle` | Invoices, allocations, credit-note applications, collectible-AR derivation |
| `customer.financial_position` | **Read-only projection** combining those authoritative inputs |
| `financial.access_resolution` | Suspension/restoration decisions, using **explicitly named quantities** |
| `VAS` | Its separate wallet, with atomic or outbox-backed transfers into billing |
| `ERP` | Accounting journals generated from synchronized Sub facts |

The four competing "balance" definitions (§2) are resolved by *naming the quantity each decision
uses*, not by collapsing them into one number. `financial.access_resolution` must state which
quantity it suspends on and which it restores on — F6/F8 exist because it currently uses one to
suspend and a different, structurally-unfailable one to restore.

**The ADR must explicitly decide when an issued credit note becomes spendable.** F4 is
unresolvable without that decision: today an issued credit note is spendable against one balance
definition and invisible to the other.

## 11. Cross-system synchronization contract

For **every** Sub→ERP and Sub→CRM flow, document:

- Originating owner
- Projection owner
- Immutable event identity and idempotency key
- Fields the receiver may **mirror**
- Fields the receiver may **derive**
- Fields the receiver must **never overwrite**
- Ordering dependencies
- Replay and reconciliation behaviour
- Correction mechanism

This directly governs F22: a billing projection must never overwrite CRM subscriber status.
`sync_flow_ownership` (§9) is the existing pattern to generalise — it already names the owning
app per flow and refuses to send flows it does not own.

## 12. Recommended sequence

Ordered by customer harm and monetary integrity. Each is one coherent domain slice per the
standard. **Every item ships with four parts: containment, forward fix, historical repair, and a
regression test.**

1. **Monetary integrity — committed loss and AR corruption.** F25 (wallet debit lost on crash →
   atomic/outbox transfer), F3 (admin re-allocation corrupts AR), F19 (import wizard orphan cash),
   F18 (reseller bulk payment credits balance while invoices stay unpaid), F2 (dead-letter replay
   discards money and silences its own alarm).
2. **Close the free-service and wrongful-suspension holes.** F6, F7, F8 — gate the four restore
   sites on the quantity the sweep suspends on; remove `"payment"` from `ALLOWED_RESTORERS[prepaid]`;
   give the prepaid sweep dunning's shield + health re-check. **Do not schedule
   `unwall_paid_accounts`** until the thresholds are unified.
3. **Make the credit ledger append-only.** F1 — fix `reverse()`, remove `update()`/`delete()`,
   settle `is_active` semantics.
4. **Multi-subscription access loss.** F10 — remove the whole-subscriber RADIUS wipe; route the
   admin/catalog path through the same enqueue the event handler uses.
5. **Settlement holes.** F15 (`Payments.update` reopens the `paid_at` regression), F17 (partial
   refund reverts invoice to unpaid), F16 (reseller proof lock → double-credit).
6. **ERP reconciliation and repair planning.** F5 — post eligible synchronized invoices
   idempotently before dependent payments; backfill and adjust existing journals.
7. **Invoice-lifecycle owner + adapter DTOs.** F11, F12, F13, F14.
8. **Migration/cutover dependency.** F23 — Splynx mirror is load-bearing and `config.py:49-55`
   claims otherwise; the memo-prefix heuristic must stop being a balance determinant.
9. **Projections.** F22 (CRM status clobber), F20/F21 (duplicate emails, portal self-contradiction),
   F24 (reporting disagrees with the ledger).

---

## 13. Remediation matrix

Every finding carries four parts. **Historical repair scope must be quantified with a read-only
prod query first** — the "possible impact" caveat in §3 applies to every row not marked confirmed.

| # | Containment (stop the bleeding) | Forward fix | Historical repair | Regression test |
|---|---|---|---|---|
| **F25** | Feature-gate `pay_bill` if crash window is unacceptable | Outbox or single-transaction transfer wallet→billing | Query wallet debits with no matching Payment; re-credit | Kill the process between debit and `Payments.create`; assert no loss |
| **F3** | Remove the "change invoice" control from the admin payment edit form | Route re-allocation through `_apply_payment_allocation` + `_recalculate_invoice_totals` + ledger | Find payments whose ledger `invoice_id` ≠ allocation `invoice_id`; re-derive both invoices | Re-point a payment; assert both invoices recalc and ledger follows |
| **F19** | Disable the payments module of the import wizard | Route imports through `Payments.create` | Find `Payment` rows with no allocation and no ledger entry; settle or void | Import a CSV row; assert allocation + ledger + `paid_at` exist |
| **F18** | — | Pass `status=succeeded` explicitly in `PaymentCreate` | Find `pending` reseller payments holding ledger credit; resolve status | Un-mock `record_bulk_payment` (`test_billing_consolidated_web.py:31`); assert invoices settle |
| **F2** | Alert on `replayed` dead letters (count them in the health gate) | Replay must pass `_extract_settlement` fields | Re-replay every row currently marked `replayed`; post missing settlements | Dead-letter a `charge.success`, replay, assert Payment created |
| **F6/F8** | — | Gate the 4 restore sites on the quantity the sweep suspends on; drop `"payment"` from `ALLOWED_RESTORERS[prepaid]` | Find accounts restored with balance < threshold (still active, unfunded) | Underfunded top-up on suspended prepaid → assert **not** restored |
| **F7** | — | Lift dunning's shield + health re-check into `_suspend_account` | Cross-check suspended accounts against active arrangements/proofs; restore wrongly-cut | Sweep an account with an active arrangement → assert **not** suspended |
| **F1** | — | `reverse()` posts reversal only, leaves original active; drop `update()`/`delete()` + the DELETE route | **Audit rows where a reversal AND an inactive original coexist — each is a double-swing to correct** | Reverse a credit; assert balance moves by exactly the amount, once |
| **F10** | — | Delete whole-subscriber RADIUS deleters; route through `refresh_radius_from_subs` | None (transient, self-heals on `populate()` sweep) | Suspend one of two subscriptions; assert the other keeps auth |
| **F15** | — | Reuse `mark_status`'s transition table + `paid_at` stamp in `update()` | Find `succeeded` payments with NULL `paid_at` | `PATCH` pending→succeeded; assert `paid_at` set + event emitted |
| **F17** | — | `_recalculate_invoice_totals` must count `partially_refunded` net of refund | Find invoices reverted to overdue by a partial refund | Partial-refund a paid invoice; assert it stays paid less the refund |
| **F16** | — | Lock + status re-check in `_verify_consolidated_proof`; stamp `provider_id` so the unique index bites | Find duplicate reseller payments by proof reference | Concurrent double-verify → assert one payment |
| **F5** | — | GL-post eligible synchronized invoices idempotently before dependent payments | Backfill + reversal/adjustment journals in ERP | Sync invoice+payment; assert AR control nets to zero |
| **F22** | — | Send only fields the projection owns; CRM must never derive `status` from a billing payload | Re-sync CRM subscriber status from Sub | Billing push → assert CRM `status` unchanged |

## 14. Registry corrections needed

`app/services/sot_relationships.py` currently asserts a chain the code does not use:

- `financial.ledger` — owner of nothing; either absorb the 16 posting sites or move the entry to
  `billing/_common` and give it a real posting API (`post(entry, *, idempotency_key, lock=True)`).
- `access.control_resolution` — no billing/enforcement callers; it is a projection helper.
- `access.radius_state` — dead code behind an unregistered flag (F9).
- **Missing entirely:** an invoice-lifecycle owner, and the VAS wallet.
