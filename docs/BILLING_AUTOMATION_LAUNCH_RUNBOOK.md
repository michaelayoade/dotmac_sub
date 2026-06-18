# Billing automation launch runbook

Status: **draft — automation NOT yet launched.** `billing_enabled` stays
`false` until every gate below is green. Companion to
`docs/POST_CUTOVER_HARDENING.md`.

## The hard rule

> **Billing automation launches only when
> `billing_integrity_audit.launch_blocked == false`.**

Never flip `billing_enabled=true` globally while the audit is red. The launch is
**staged**, least-risky task first, with a dry-run discipline between stages.

Gate check (read-only):

```bash
docker compose exec -T -e PYTHONPATH=/app app \
    python scripts/billing/billing_integrity_audit.py
# launch_blocked must be False; the four launch-blocking gauges must be 0
# (or explicitly waived with a documented reason + approver).
```

## Prerequisites (safety rails merged first)

| PR | what | required before launch |
| --- | --- | --- |
| #283 | IPAM consistency (audit/reconciler/repair) | yes |
| #288 | terminal-IP release forward fix | yes |
| #286 | billing-integrity audit (stacked on #283) | yes — it IS the gate |
| #285 / #287 | finance artifact PRs (deposit fallback / billing violations) | yes |

## Step 1 — clear the launch-blocking gauges

All four must be **0**, or explicitly waived with a written reason + named
approver:

- `billing_disabled_service_lines` — **money bug.** Finance dispositions the 22
  (`post_cutover_fallback...`/`billing_violations_*` worklists from #287):
  void unpaid lines, credit-note paid lines, or mark valid-historical. Sign-off
  required before automation starts compounding balances.
- `billing_duplicate_subscription_period_lines` — **money bug.** Finance keeps
  one line per group, voids the rest (51 groups / 106 lines in the snapshot).
- `active_subscription_missing_radius` — see Step 2.
- `billing_addon_without_billable_parent` — was 0 in the snapshot; keep it 0.

**These are finance decisions.** Engineering provides the gated executor
(dry-run-first, manifest + rollback — to be built like
`apply_terminal_ip_backlog.py`); it does not auto-fix money records without an
approved worklist.

Re-run the audit after remediation; do not proceed until the money gauges are 0.

## Step 2 — fix service-access blockers

Resolve the active subscriptions in `active_subscription_missing_radius` (4 in
the snapshot — one is a QA/test login). A customer must not be billed by
automation while they cannot authenticate. Either:

- provision the missing RADIUS credentials, or
- explicitly exclude known QA/test logins from the launch gate (documented).

## Step 3 — dry-run billing daily (at least a few cycles)

Before enabling any write, run the dry-run cycle daily and capture:

- subscriptions scanned
- subscriptions billed
- skipped (count)
- currency-skipped, pending-activated
- disabled/canceled billed count (must be 0) — from the integrity audit
- duplicate-period count (must be 0) — from the integrity audit

```bash
# 1. operational counts — read-only; the CLI rolls back after the dry run
docker compose exec -T -e PYTHONPATH=/app app \
    python scripts/billing/billing_dry_run_snapshot.py \
        --out /app/billing_dryrun_$(date +%F).json --prev /app/billing_dryrun_prev.json

# 2. safety gauges — disabled/canceled-billed and duplicate-period must be 0
docker compose exec -T -e PYTHONPATH=/app app \
    python scripts/billing/billing_integrity_audit.py
```

Run it **daily, including on billing-cycle days** — `subscriptions_billed` is 0
when nothing is due (e.g. between cycles, or a prepaid-dominant base), so a
zero day is only meaningful next to a billing day. **Any non-zero
disabled/duplicate gauge is a stop.**

> **Amount deltas (TODO before launch).** The snapshot records *counts only* —
> `run_invoice_cycle(dry_run=True)` does not project an invoice total (the
> dry-run branch counts without building line amounts, billing_automation.py:909).
> Finance needs **amount** deltas, not just `subscriptions_billed`. A follow-up
> must add a projected total to the dry-run summary (separate PR touching the
> cycle); until then, reconcile `subscriptions_billed` × expected price to
> finance's model manually. Do not treat count-only parity as revenue parity.

## Step 4 — launch in phases (least risky first)

Enable ONE stage at a time. After each, watch a full cycle + support/RADIUS
before advancing. `billing_enabled` is the master switch; each stage also gates
on its own scheduled task being enabled.

| stage | task | customer impact | enable when |
| --- | --- | --- | --- |
| **1. Postpaid invoice generation** | `run_invoice_cycle` | low (invoice only) | gauges 0 + dry-run clean |
| **2. Overdue marking / reminders** | overdue sweep + reminder notices | low–medium | stage 1 stable one cycle |
| **3. Dunning / block enforcement** | dunning + enforcement | **high (cuts service)** | stage 2 verified; support ready |
| **4. Prepaid drawdown** | prepaid charge task | **high (debits balances)** | stages 1–3 stable; reconcile clean |
| **5. Autopay** | autopay charges | **high (moves money)** | all prior stable |

Prepaid drawdown and dunning are customer-impacting — **never the first thing
enabled.** (Confirm the exact setting key per stage from
`app/services/scheduler_config.py` at enable time.)

## Step 5 — rollback

Single, fast rollback for any stage:

```
set billing_enabled = false   # halts run_invoice_cycle and gated writers
```

Then disable the specific stage's scheduled task. Roll back money mutations via
the remediation tool's manifest (`--rollback`), not by hand.

## Roles

- **Finance** — approves the void/credit/valid-history dispositions (Step 1) and
  reviews every dry-run revenue delta (Step 3). No automation compounds balances
  without finance sign-off.
- **Engineering/ops** — runs the gate + dry-run, flips one stage at a time, owns
  rollback.
- **Support / NOC** — monitors tickets + RADIUS auth after each stage,
  especially stages 3–5.

## Launch checklist (all must be true)

- [ ] #283, #288, #286, #285, #287 merged
- [ ] `billing_integrity_audit.launch_blocked == false` (money gauges 0 or waived)
- [ ] `active_subscription_missing_radius` resolved or QA excluded
- [ ] ≥ 3 daily dry-run snapshots, invoice total reconciles to expected
- [ ] finance signed off on remediation + expected revenue
- [ ] rollback rehearsed (`billing_enabled=false` + per-task disable)
- [ ] support/NOC on watch for the enabled stage
