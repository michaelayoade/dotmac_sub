# Billing Revenue-Leak Closure & Enforcement Consolidation

**Status:** Proposed (scoping) — 2026-06-24
**Author:** audit + design pass
**Prompt:** "Are customers renewing? Prepaid charges should be enabled. Which other settings cost us money?"

---

## 1. Context

A read-only prod audit (2026-06-24) of renewal/dunning health found that **only postpaid (118 active subs) has a working renewal loop**. The prepaid majority is unbilled, and recorded payments collapsed at the Splynx decommission.

### The numbers
| Segment | Active subs | Monthly recurring | Status |
|---|---|---|---|
| **Prepaid** | 3,965 (97%) | **₦115.5M/mo** | **Not charged at all** — drawdown + enforcement runners retired |
| Postpaid | 118 | ₦15.6M/mo | Invoiced daily; dunning works but suspends only at day 60 |
| Postpaid overdue | 38 accounts / 66 invoices | — | **₦27.8M past-due**, only 1 suspension |

**Headline leak: ₦115.5M/month of prepaid service delivered with no billing.**

### Splynx is gone — the exposure is live (not "expected")
A second audit (2026-06-24) asked whether Splynx might still be billing prepaid. **It is not.** No Splynx sync tasks remain in `scheduled_tasks`, and the last Splynx-sourced payment *and* invoice are both dated **2026-06-16**. DotMac Sub is the **sole biller of record** as of Jun 16, so the unbilled prepaid base is real revenue loss accruing now, not a handoff artifact. Recurring value of the gap reconciles to **₦115M** (subscription `unit_price`) – **₦126M** (catalog `offer_prices`).

### Why prepaid is off (deliberate, not a stray flag)
`scheduler_config.py:762-781` hardcodes `prepaid_charges_runner` and `prepaid_enforcement_runner` to `enabled=False`:
> *"RETIRED: deposit-based prepaid enforcement suspended paid customers on a stale Splynx deposit … Forced OFF here (hardcoded) so it can never be re-enabled by a setting/env default."*

The drawdown read its balance from `_resolve_prepaid_available_balance` (`collections/_core.py:89`), which **falls back to the stale `subscribers.deposit`** (Splynx-sourced). With Splynx decommissioned that deposit is frozen, so charging/enforcing against it overcharges and wrongly suspends paying customers. **Flipping the `scheduled_tasks` row does nothing** (scheduler re-forces off) and would be unsafe.

### What changed since retirement — the blocker is mostly gone
- **3,904 of 3,907** active prepaid accounts already carry a `"Prepaid opening balance @ cutover"` ledger entry. Only **3** are unseeded.
- So the local ledger now *has* a balance source of truth for nearly the entire prepaid base. The remaining work is cleanup + flipping the read away from the deposit fallback — not a from-scratch seed.

---

## 2. Architecture decision: one enforcement reconciler, two accrual engines

The enforcement **action** is already unified — both prepaid and postpaid converge on `account_lifecycle.suspend_subscription` / `restore_subscription` → `EnforcementLock` → `subscription_suspended` event → RADIUS reject. The duplication is in the **assessment + orchestration**:

| Layer | Prepaid | Postpaid | Unify? |
|---|---|---|---|
| Accrual (what's owed) | `run_prepaid_charges` drawdown | `run_invoice_cycle` | **No** — different mechanics, but both must write the **same ledger** as the single balance truth |
| Enforcement (suspend/restore) | `PrepaidEnforcement` | `DunningWorkflow` (+ event-driven `auto_suspend_on_overdue`) | **Yes** — collapse to one reconciler |

`service_status.build_service_status()` (`service_status.py:109`) already computes one mode-aware delinquency verdict. **Target:** one `billing_enforcement_reconciler` that iterates all active subs, gets that verdict, and is the *sole writer* converging enforcement state — emitting a per-account `{mode, verdict, enforced?, drift}` audit gauge. This is the same "one reconciler = sole writer, audit-gauge-first" strategy in `CONNECTIVITY_STATE_MACHINE.md`; billing delinquency is its trigger input.

### One engine, *not* one policy — the prepaid and postpaid ladders are deliberately different
Unifying the enforcement **writer** does **not** merge the **policies**. The `policy_sets` / `policy_dunning_steps` tables already model per-mode ladders (and support per-account/reseller/offer overrides, currently unused = 0). Prod has two active, distinct, named sets:

| Policy set | Ladder | Why |
|---|---|---|
| **Default — Prepaid (immediate suspend)** (`…d001`) | day 0 → **suspend** | pay-before: balance hits zero → cut now, no grace |
| **Default — Postpaid (suspend at 60 days)** (`…d002`) | 7 notify → 30 notify → 60 suspend | pay-after: escalate, then cut |

Mode→policy is bound via the `default_prepaid_policy_set_id` / `default_postpaid_policy_set_id` settings. The reconciler reads whichever set the account resolves to — policy stays data/config, one engine applies it. **Two consequences:**
- §4B "tighten policy" applies **only to the postpaid set (`…d002`)**. The prepaid set is already day-0/no-grace — do not loosen it.
- The prepaid day-0/no-grace ladder is exactly *why* running it against the **stale Splynx deposit** caused mass wrongful suspensions (zero grace × wrong balance = instant cut). The aggressive prepaid policy is correct for prepaid semantics, which makes ledger-balance correctness (§3 Phase 0/1) a **hard prerequisite** before the prepaid policy is ever armed again.

There are currently **three** postpaid suspend triggers that must be reconciled into the single reconciler:
1. `DunningWorkflow` day-60 policy `suspend` step (active),
2. event-driven `_handle_invoice_overdue` gated by `auto_suspend_on_overdue` (off) **and** by overdue events that only fire if `overdue_check_enabled` is on (off),
3. the retired prepaid path.

---

## 3. Prepaid-on-ledger cutover (the main project)

Goal: charge and enforce prepaid against the **local ledger only**, never the stale deposit.

**Phase 0 — reconcile the seed (data correctness).** The opening-balance seed had bugs — duplicate cutover debits and manual reversals appear in `ledger_entries` memos. Run an audit that, per active prepaid account, recomputes `available_balance = Σcredits − Σopen_invoice_balance` from the ledger and compares to `subscribers.deposit`; flag mismatches. Fix duplicate/reversed seed rows. Seed the 3 unseeded accounts.

**Phase 1 — remove the deposit fallback.** In `_resolve_prepaid_available_balance` (`collections/_core.py:89`) drop the `splynx deposit` branch so balance is purely ledger-derived. Guard with a one-time assertion that every active prepaid account has an opening-balance entry (else skip + alert, never fall back to deposit). (The "seed → remove → guard" sequence from `post-cutover-hardening`.)

**Phase 2 — shadow / dry-run drawdown.** `run_prepaid_charges(db, dry_run=True)` over the full base; produce a report: who would be debited, new balances, who would drop below `min_balance`, who would suspend. Review against expectations before any real debit. The engine is already idempotent (per-`(subscription, charge_date)` memo token, row lock).

**Phase 3 — un-retire the runners, charges + enforcement together.** Remove the hardcoded `enabled=False` in `scheduler_config.py:767-781`; drive both from settings (`prepaid_charges_enabled`, `prepaid_enforcement_enabled`, both with `*_expected` guards à la `billing_switch_guard`). **Never enable charges without enforcement** — charges alone only records debits no one acts on (no revenue recovered) while building negative balances.

**Phase 4 — policy.** Revisit `prepaid_default_min_balance` (currently `0` → only suspends at empty/negative), `prepaid_grace_days=0`, `prepaid_deactivation_days=0`. Decide whether a small positive `min_balance` (warn-before-empty) is wanted. Enable `customer_balance_notifications_enabled` **only after** Phase 1 (before that it warns off stale data).

**Phase 5 — fold into the unified reconciler** (Section 2).

**Gate before any real prepaid debit/suspend:** Phase 0 clean + Phase 2 dry-run reviewed + explicit rollout sign-off.

---

## 4. Quick-win money knobs (independent of the prepaid project)

| # | Change | Where | Note / dependency |
|---|---|---|---|
| **A** | Enable postpaid auto-suspend: set **both** `overdue_check_enabled=true` and `auto_suspend_on_overdue=true` | `domain_settings` (billing) | Coupled — auto-suspend fires on overdue events that only exist if overdue_check runs. Blast radius: **38 accounts / ₦27.8M**. 48h grace warning built in (`suspension_grace_hours`). |
| **B** | Tighten dunning policy — move `suspend` earlier than day 60 (e.g. notify 7 / throttle 14 / suspend 21) | `policy_dunning_steps` (set `0d…d002`) | Currently 7 notify → 30 notify → 60 suspend = ~60 days free. Pairs with A. |
| **C** | Re-enable customer comms = turn on **`notification_queue_runner`** (`notification_queue_enabled`) | `scheduler_config.py:973` / setting | **Not** `send_billing_notifications`. **Risk:** ~2,192-SMS backlog will blast on enable — triage/purge or throttle first. This is the biggest *renewal-recovery* lever. |
| **D** | Backfill `unit_price` on **54 zero-price active prepaid subs** from `offer_prices` | data fix | Mostly Unlimited plans (Basic 16, Compact 11, …). A few IP add-ons (`/29 IP`, `/32 IP`) may legitimately bundle — review before backfill. Even after prepaid billing is fixed these stay free. |
| **E** | Add Flutterwave keys (currently blank) | `domain_settings` (billing) | Secondary provider + `payment_gateway_failover_enabled=true` but keyless → failed Paystack charges aren't recovered. **Needs keys from ops.** |

**Do NOT touch now:** `customer_balance_notifications_enabled` (couple to prepaid Phase 1); raw force-enable of prepaid runners.

### Data-integrity & hardening items (from the 2026-06-24 sole-biller audit)
| # | Finding | Detail | Action |
|---|---|---|---|
| **F** | **23 paid invoices with non-zero balance** | All underpaid-marked-paid, **₦411,821.25** total. Corrupts dashboards, aging, collections, and restore logic that trusts `status=paid`. | Reconcile before trusting any billing dashboard: re-derive status from `balance_due` (paid ⇔ balance≤0) and either re-open or write off the residual per ledger truth. |
| **G** | **Billing-mode drift** | **118 active subs = sub `postpaid` / offer `prepaid`** — the *entire* active postpaid population sits on prepaid-configured offers (`subscription_vs_account` drift = 0). `Subscription.billing_mode` is load-bearing so they bill postpaid today. | Run `billing_mode_audit.find_billing_mode_inconsistencies`. **Explicit decision required at cutover:** do these 118 stay postpaid, or revert to their offers' prepaid mode? This must be settled *before* prepaid drawdown arms, or 118 accounts could flip billing model unexpectedly. |
| **H** | **Live secrets in plaintext settings** | `paystack_secret_key=sk_live_…`, gateway keys, etc. stored in `domain_settings` / env rather than OpenBao secret-refs. | Rotate exposed live keys, move to OpenBao refs (`openbao-token-incident` infra exists), keep only refs in DB/env. |

### Prepaid billing model: split by period (decided 2026-06-24)
The active prepaid base splits cleanly by billing period, and each period wants a different engine:

| Prepaid period | Active | Due now | Configured recurring | Engine |
|---|---|---|---|---|
| **Daily** | 2,605 | 214 | ₦46.58M | **Drawdown** — `run_prepaid_charges` debits the ledger daily; balance exhausted → day-0 prepaid policy suspends |
| **Monthly** | 1,360 | 167 | ₦72.51M | **Invoice-in-advance** — `prepaid_monthly_invoicing_enabled=true` → `run_invoice_cycle` issues a prepaid invoice (due on issue) |

A dry-run on 2026-06-24 found **201 prepaid subs due/new today, 193 chargeable, ₦4,352,062.50**. Daily→drawdown, monthly→invoice-in-advance. The drawdown path is the one un-hardcoded by the scheduler patch below; monthly invoicing is a separate settings flip into the existing postpaid machinery.

### Scheduler patch (branch `feat/prepaid-charges-setting-gated`)
`scheduler_config.py:774` previously hardcoded `prepaid_charges_runner` to `enabled=False` so no setting could revive it. The patch replaces that with a `prepaid_charges_enabled` setting (collections, **default False**, env `PREPAID_CHARGES_ENABLED`), mirroring `dunning_enabled`. Deploying it is **inert** (still off) but makes the drawdown engine flippable for a controlled cutover instead of a code edit. **`prepaid_enforcement_runner` stays hardcoded off** — it reads the deposit-fallback balance and the prepaid policy is day-0/no-grace, so it cannot be re-armed until §3 Phase 1 lands.

### Supervised one-time charge runbook (daily prepaid)
1. `run_prepaid_charges(db, dry_run=True)` → export the ~193 affected accounts + amounts; eyeball against expectations.
2. Run **once, manually**, charges-only (enforcement stays off) → this *accrues* the debt, stops the leak compounding, suspends no one. Idempotent per `(subscription, charge_date)`.
3. Verify ledger debits + balances; spot-check accounts.
4. Only then flip `prepaid_charges_enabled=true` to schedule the daily cadence.
5. Enforcement remains a *separate, later* step gated on §3 Phase 1 (deposit-fallback removal).

---

## 5. Billing-liveness audit (the "are we fully in production?" guard)

Standing check (script + gauge + alert) asserting invariants — this is what would have caught all of the above:
1. **Coverage** — every `active` sub maps to exactly one *enabled* billing path. *(Today: 3,965 prepaid → 0 enabled = red.)*
2. **Scan completeness** — `billing_runs.subscriptions_scanned` ≈ eligible active subs. *(Today 108 vs 4,083 = red.)*
3. **Runner SLA** — each runner recorded a *success* in its window. *(Fix: `scheduled_tasks.last_run_at` is never populated — observability gap.)*
4. **Flag-vs-expected** — every billing flag matches its `*_expected` (generalize `billing_switch_guard`).
5. **One enabled row per task** — no duplicate/ambiguous `scheduled_tasks`. *(Today: dup `billing_runner`/`dunning_runner`/`overdue_checker` pairs.)*
6. **Enforcement drift** — count active subs where computed verdict ≠ actual RADIUS/lock state.
7. **Money continuity** — payments in last 24h within X% of trailing-7-day avg. *(Would have caught the Splynx-channel death on Jun 17.)*

### Other hygiene fixes found
- `dunning_enabled` has contradictory `value_text='true'` / `value_json='false'` (resolves true via `extract_db_value`, `settings_spec.py:3073`) — reconcile the row.
- Duplicate `scheduled_tasks` rows (enabled+disabled pairs) — dedupe.

---

## 6. Sequencing

1. **Now (safe):** build the billing-liveness audit (§5) — read-only, quantifies gaps, becomes the regression guard.
2. **Quick wins (§4):** A+B (postpaid auto-suspend + policy) after previewing the 38-account blast; C after SMS-backlog triage; D after price review; E when ops supplies keys. All customer-facing — flip deliberately, not via raw SQL.
3. **Prepaid cutover (§3):** Phase 0→2 (all read-only/dry-run) → review → Phase 3 enable.
4. **Consolidation (§2):** unified reconciler once both modes are live.

## 7. Verification
- Prepaid Phase 2 dry-run report reviewed line-by-line before Phase 3.
- After §4A/B: watch `enforcement_locks(reason=overdue)` and overdue-invoice ₦ trend down; confirm no paying customers suspended (drift gauge = 0).
- After §4C: confirm SMS send rate throttled; watch native payment volume recover toward the pre-cutover ~33/day.
- Billing-liveness audit (§5) green across all 7 invariants = "fully in production".
