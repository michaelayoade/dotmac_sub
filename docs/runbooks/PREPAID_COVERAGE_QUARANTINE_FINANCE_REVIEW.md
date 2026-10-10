# Prepaid coverage quarantine finance review

Work item and alert owner label: `financial-billing`.
Evidence owner: `financial.prepaid_service_coverage_reconciliation`.
Read-only diagnostic: `scripts/billing/diagnose_prepaid_coverage_quarantine.py`.

Use this runbook for an open `prepaid-coverage:quarantine:<account_id>` work
item (admin alert "Prepaid coverage evidence needs finance review") or the
`PrepaidCoverageQuarantinedEvidence` alert, when the work item's
`reason_codes` include `malformed_paid_invoice_period` or
`malformed_renewal_origin`. The other blocking codes
(`conflicting_financial_sources`, `ambiguous_paid_invoice_lines`,
`ambiguous_renewal_adjustments`) use the same work item; their routing is in
`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`.

## What the quarantine means

The prepaid sweep only suspends a service when it can prove the customer has
not paid for the current period. When an account has broken payment records,
that proof isn't possible, so the account is quarantined. This protects the
customer: a quarantined service is never suspended automatically. The account
stays protected until someone fixes the source records through their owner.
Each work item has a 72-hour finance SLA (`sla_due_at`). Overdue items raise
`PrepaidWorkItemsOverdue`.

### `malformed_paid_invoice_period`

A fully paid invoice, with a charge line linked to the customer's prepaid
service, does not record which service period it paid for. Either the start or
end date is missing, or the end is not after the start. The system can't tell
whether that payment covers today, so it won't suspend the service. One such
invoice is enough to keep that service quarantined, however old it is,
whenever the service has no other current coverage.

### `malformed_renewal_origin`

A prepaid renewal was charged directly against the customer's balance (an
account adjustment) without its normal invoice. Its machine-readable reference
(`origin_ref`) should say `<subscription id>:<period start>:<period end>`.
This code means one of the following:

- the reference is missing or not in that format;
- the period it names ends on or before its start; or
- the adjustment disagrees with its ledger debit on account, amount, or
  currency.

The system can't tell which service or period that charge paid for, so every
uncovered prepaid service on the account is quarantined.

## 1. Run the read-only diagnostic

Run it on the explicitly approved application host with the deployed image.
The command can't write. It runs in one `REPEATABLE READ, READ ONLY` snapshot,
rolls back, and prints `financial_state_changed: false`.

```bash
# Every open quarantine work item:
poetry run python -m scripts.billing.diagnose_prepaid_coverage_quarantine
# One account, machine-readable (attach this JSON to the finance ticket):
poetry run python -m scripts.billing.diagnose_prepaid_coverage_quarantine \
  --account-id <account-uuid> --json
```

Exit code `2` means at least one finding. `0` means nothing malformed remains.
For each account, it reports:

- the work item: status, reason codes, and SLA;
- each collectible prepaid service, with its current reconciliation decision
  and reason. These come from the owner's own preview.
- for `malformed_paid_invoice_period`, one entry per invoice:
  - the invoice ID and number, the Splynx ID, and which period field is wrong
    (`missing_start_and_end`, `missing_start`, `missing_end`, or
    `end_not_after_start`);
  - the total, balance due, allocated payments, and applied credit notes;
  - every line, with its subscription, amount, `kind`, and structured
    metadata period;
  - the entitlements created from each line;
  - `period_proof`, which says what the structured evidence proves.
- for `malformed_renewal_origin`, one entry per adjustment:
  - the adjustment ID, raw `origin_ref`, and the parsed subscription, start,
    and end;
  - the failed `defects`;
  - the adjustment and its ledger debit, side by side;
  - any entitlements linked to that debit.
- the resolution options for each finding. `SANCTIONED` options name a
  reviewed owner, runbook, and prefilled read-only preview command. `NO
  SANCTIONED REPAIR` options state the missing capability.

Each option depends on a fact that only Finance can establish, given in its
`when` field. The diagnostic never decides that fact. It never reads memo,
description, or note text as evidence.

## 2. Decide each finding

### A. `malformed_paid_invoice_period`

The sanctioned repair is the four-eyes paid-invoice period repair (owner
`financial.prepaid_paid_invoice_period_repair`, permission
`billing:prepaid_reconciliation:repair`, CLI
`scripts/billing/repair_prepaid_paid_invoice_period.py`). Finance states which
subscription and which service period the payment bought; the owner checks
that statement against the records and restores the period and its coverage
together. It never changes money, the invoice status, allocations, ledger
entries, or the line's `kind`.

First decide which case applies. The diagnostic's
`reviewed_paid_invoice_period_repair` option carries a prefilled read-only
preview command.

1. **The period is proven by structured data.** `period_proof` is
   `source_entitlement` or `line_metadata_period`. The prefilled command already
   contains `proven_period_start`..`proven_period_end`. Finance confirms that is
   the period the payment bought.
2. **The period is not proven.** `period_proof` is `none`, `conflicting`, or
   `derived_line_metadata_period` (a derived period was inferred from the
   payment date, so it is not proof). Finance determines the paid period from
   source documents: the original or Splynx invoice, the payment receipt, and
   the customer order. Memo or description text is never a source.
3. **A line is not a service charge.** The line `kind` is not
   `base_subscription` and Finance confirms from source documents that it was a
   one-off charge (installation, equipment, a fee) that bought no service
   period. There is still no sanctioned repair: route
   `engineering_non_service_line_classification`. Do **not** invent a period to
   clear the quarantine. A proration (upgrade) charge did buy service for the
   upgraded days; Finance may repair it with that exact period, acknowledging
   the warnings below.

#### Preview (read-only)

```bash
poetry run python -m scripts.billing.repair_prepaid_paid_invoice_period preview \
  --invoice-id <invoice> --line-id <line> --subscription-id <subscription> \
  --period-start <ISO-8601 with offset> --period-end <ISO-8601 with offset>
```

Exit `0` means actionable; `2` means blockers remain. The JSON shows:

- `before`/`after`: the invoice period and the line's subscription and period;
- `planned_entitlement`: the entitlement the paid-line entitlement writer will
  create (`create_from_paid_line`), or the existing one that already funds the
  payment (`existing_entitlement_funds_line`);
- `overlapping_entitlements`, `terms`, `settlement`;
- `warnings` and `blockers`;
- `quarantine_effect`: current and projected blocking reasons, and
  `work_item_resolves_on_next_sweep`;
- `fingerprint`.

**Blockers** (fix the cause or escalate; they cannot be acknowledged):

| Blocker | Meaning and route |
|---|---|
| `invoice_not_paid` | Not an active, non-proforma, fully paid invoice. Not this runbook. |
| `invoice_period_not_malformed` | The period is already valid. Nothing to repair. |
| `settlement_does_not_match_total` | Active payment allocations plus applied credit notes do not equal the invoice total (for example, over-allocation). Correct the allocation through its owner first; Finance decides where the excess belongs. |
| `currency_mismatch` | Invoice currency is not the prepaid enforcement currency. Escalate. |
| `subscription_account_mismatch`, `subscription_not_prepaid`, `line_linked_to_other_subscription` | The proposed subscription is wrong for this invoice or line. |
| `invoice_has_other_subscription_lines` | The invoice has a second subscription-linked charge; the invoice-level period would assert both. Escalate. |
| `line_already_has_entitlement` | An entitlement is already sourced from this line. Use `--adopt-entitlement-id` with that entitlement. |
| `overlapping_entitlement_unresolved` | An active entitlement for the subscription overlaps the period. See "Overlaps" below. |
| `adopted_entitlement_*` | The entitlement named with `--adopt-entitlement-id` is not active for this subscription, is not structurally linked to this invoice or line, has a different period, or a different currency. |
| `unacknowledged_warning`, `acknowledged_warning_not_present` | Acknowledge exactly the warnings shown, no more. |

**Warnings** (each needs `--acknowledge-warning <name>` after Finance confirms
it from source documents):

- `line_not_base_subscription`: the line is not tagged as a base subscription
  charge (for example a proration). Finance confirms it bought this period.
- `amount_differs_from_subscription_terms`: the line amount differs from the
  subscription's `unit_price`.
- `subscription_terms_unpriced`: the subscription has no contracted amount.
- `period_not_one_billing_cycle`: the period is not exactly one billing cycle.
- `adopted_entitlement_amount_differs`: the adopted entitlement's funded amount
  differs from the line amount.

**Overlaps.** If an active entitlement already funds *this* payment (the
diagnostic or the entitlement's structured `paid_invoice_id` shows it), pass
`--adopt-entitlement-id <id>` with exactly its period. No second entitlement is
created, so the payment is not counted twice. If a different entitlement
legitimately coexists (for example a base cycle under an upgrade proration),
pass `--acknowledge-overlap <id>` for each; the owner retains it and creates the
new one. Anything else is an escalation.

#### Request (staff member 1)

Re-run with `request`, the same proposal arguments, and the preview
fingerprint:

```bash
poetry run python -m scripts.billing.repair_prepaid_paid_invoice_period request \
  <same proposal arguments> --fingerprint <sha256> \
  --reason "<Finance determination and documents relied on>" \
  --evidence-ref <finance-ticket-or-document-ref> \
  --evidence-sha256 <sha256 of the evidence file> \
  --actor <system-user-uuid> --idempotency-key <unique-key>
```

The request changes no invoice. It records the proposal, both acknowledgements,
the reason, and the evidence SHA-256 (event
`prepaid_paid_invoice_period_repair.requested`). It is refused when the
fingerprint is stale or any blocker remains.

#### Approve (staff member 2, a different person)

```bash
poetry run python -m scripts.billing.repair_prepaid_paid_invoice_period list
poetry run python -m scripts.billing.repair_prepaid_paid_invoice_period approve \
  --request <request-id> --fingerprint <same sha256> \
  --approver <different-system-user-uuid> --idempotency-key <unique-key>
```

Self-approval is refused. Under lock, the owner recomputes the preview and
requires the identical fingerprint; any change since the request returns
`stale_preview` and needs a new request. It then, in one transaction:

- writes the period on the invoice and the line (and links an unlinked line to
  the subscription) through the invoice owner;
- creates the entitlement through the paid-line entitlement writer, or records
  the adopted one;
- records an audit row (`repair_paid_prepaid_invoice_period`) and the event
  `prepaid_paid_invoice_period.repaired`, with both staff identities.

Re-running the same approval returns the stored outcome (`replayed: true`).
Exit code `3` means the owner refused; the JSON `error` names why.

These existing tools still **do not** fix this code. Do not run them hoping
they will:

- the admin invoice "prepaid coverage reconciliation" screen and
  `scripts/billing/prepaid_coverage_reconcile.py`, which only create
  entitlements from an exact period;
- `reconcile_prepaid_drafts --repair-paid-invoice`, which only repairs an
  unlinked line with an exact settlement period;
- `REVIEWED_PREPAID_INVOICE_SEQUENCE_RECONSTRUCTION.md`, which refuses paid
  invoices;
- calendar reconciliation, which requires a stored period;
- `LEGACY_PREPAID_RENEWAL_TAX_INVOICE_CORRECTION.md`, which is for renewal
  adjustments only.

### B. `malformed_renewal_origin`

1. **Defects include `ledger_account_mismatch`, `ledger_amount_mismatch`, or
   `ledger_currency_mismatch`.** There is no sanctioned repair. The adjustment
   and its ledger debit disagree, and every reversal owner refuses inconsistent
   debit evidence. Route: `engineering_adjustment_ledger_repair`.
2. **Exactly one active, non-invoice entitlement with the same amount is
   linked to the debit.** Finance must establish whether the customer received
   service for that entitlement's period.
   - **Service was not received.** This route is sanctioned. Follow
     `docs/runbooks/UNUSED_PREPAID_RENEWAL_CORRECTION.md` (owner
     `financial.prepaid_service_renewals`). Start with the diagnostic's
     prefilled read-only command:
     `billing_target_shadow preview-unused-prepaid-renewal-correction`.
     Continue only if the preview reports `actionable=true`. Apply with
     `correct-unused-prepaid-renewal`. The owner reverses the debit and the
     entitlement together. The reversed adjustment no longer counts as
     evidence.
   - **Service was received.** The debit is legitimate and only its reference
     is wrong. Route: `reviewed_renewal_origin_correction` (see "Renewal origin
     correction" below). The linked entitlement proves the correct reference,
     which the diagnostic shows.
3. **No entitlement is linked to the debit.**
   - **Finance confirms the debit was raised in error** (it bought no service
     period). This route is sanctioned: a reviewed account-adjustment reversal
     (owner `financial.account_adjustments`, permission
     `billing:ledger:write`). Call
     `POST /api/v1/account-adjustments/<adjustment-id>/reversal/preview` with
     the approval reference as `reason`. Confirm with `POST .../reversal`,
     passing the exact `preview_fingerprint` and a unique `idempotency_key`.
   - **The debit is legitimate** (the customer received the service). Route:
     `reviewed_renewal_origin_correction`: Finance names the existing
     entitlement the debit funded, or supplies the subscription and exact period
     when none exists.
4. **Exactly one active entitlement is linked, but it is invoice-backed or its
   amount differs from the debit** (for example a pre-tax entitlement beside a
   tax-inclusive debit). The unused-renewal correction does not apply. If the
   service was received, route `reviewed_renewal_origin_correction` with
   `entitlement_already_linked`; the owner asks Finance to acknowledge the
   amount and invoice-backing warnings.
5. **Any other linked shape** (several entitlements, an inactive or foreign
   entitlement) has no sanctioned repair. Escalate it
   (`engineering_renewal_origin_correction`).

#### Renewal origin correction

Owner `financial.prepaid_renewal_origin_correction`, permission
`billing:prepaid_reconciliation:repair`, CLI
`scripts/billing/correct_prepaid_renewal_origin.py`. Two steps, no money moves.

```bash
# 1. Read-only preview (exit 2 while blockers remain):
poetry run python -m scripts.billing.correct_prepaid_renewal_origin preview \
  --adjustment-id <adjustment> --disposition entitlement_already_linked \
  --entitlement-id <entitlement> [--acknowledge-warning <warning> ...]
# 2. Confirm, restating the preview fingerprint:
poetry run python -m scripts.billing.correct_prepaid_renewal_origin confirm \
  <same proposal arguments> --fingerprint <sha256> \
  --reason "<Finance determination and documents relied on>" \
  --evidence-ref <finance-ticket-or-document-ref> \
  --evidence-sha256 <sha256 of the evidence file> \
  --actor <system-user-uuid> --idempotency-key <unique-key>
```

Dispositions:

- `entitlement_already_linked`: exactly one active entitlement is already
  linked to the debit. Its subscription and period are the canonical reference.
- `link_existing_entitlement`: Finance names one existing active entitlement the
  debit funded that carries no ledger-debit link (`--entitlement-id`). The owner
  records the link through the entitlement writer's flush-only participant. No
  coverage is created or extended.
- `create_entitlement_from_debit`: no entitlement exists. Finance supplies
  `--subscription-id`, `--period-start`, `--period-end`; the entitlement is
  created only through the existing wallet-debit entitlement writer. An active
  entitlement overlapping the period blocks it unless named with
  `--acknowledge-overlap`.

The preview shows `origin_ref_before`/`origin_ref_after`, the planned entitlement
action, `warnings` (acknowledge exactly those shown, after Finance confirms them
from source documents), `blockers`, and `quarantine_effect`
(`malformed_adjustment_ids_before`/`after`, `projected_blocking_reasons`,
`work_item_resolves_on_next_sweep`). Blockers include a reversed, non-renewal, or
ledger-inconsistent adjustment; an already canonical reference; an inactive,
foreign, or other-debit-linked entitlement; and a canonical reference already
carried by another adjustment. Three further blockers cannot be acknowledged:
`entitlement_invoice_already_settled` (link mode: the entitlement's source
invoice is already fully settled by payments, credit notes or opening
consumption, so the wallet debit would fund the period twice);
`would_make_invoice_documentary` (the change would add a paid prepaid invoice
with the same subscription, period, amount and currency to the direct-renewal
documentary set, silently removing its customer-position consumption; the
preview lists `position_impact.invoices_made_documentary` and the
`prepaid_available_balance` before/after, and Finance must decide the invoice
first); and, in create mode, `period_not_one_billing_cycle`,
`period_exceeds_one_billing_cycle`, `period_start_outside_debit_cycle` (start
more than one cycle from the debit's effective date) and
`cycle_already_covered_by_invoice` (an invoice-backed entitlement or paid
invoice already covers the period). `position_impact` also shows the current
coverage end before/after. Re-running the same confirmation returns the
stored outcome (`replayed: true`); a stale fingerprint returns `stale_preview`.
Exit code `3` means the owner refused; the JSON `error` names why.

Every sanctioned route returns the debit to the customer's prepaid funding.
Use one only when Finance has decided the charge itself was wrong, never just
to clear the quarantine.

## 3. Approvals and evidence

Each correction has two roles:

- a **Finance approver**, who decides the fact in the option's `when` from
  source documents; and
- a separate **operator**, who runs the preview and apply.

The two must be different people. The paid-invoice period repair enforces this
in code (request and approve by different staff, both recorded). The other
owners record one actor; for them the approver is recorded through the approval
reference in `--reason`/`reason` and in the finance ticket.

Record the following in the finance ticket:

- the diagnostic JSON for the account;
- the approver's decision and the documents they relied on;
- the owner preview output and its fingerprint;
- the idempotency key, and the actor (`user:<operator-uuid>`).

Don't put customer names, emails, phone numbers, or private documents in
command arguments, logs, or source control.

For an engineering escalation, attach the diagnostic JSON, the route name,
Finance's determination, and the proposed values if any. Don't propose a
period that came from memo text.

## 4. Closure

You don't close the work item by hand. The prepaid balance sweep runs hourly
by default (`prepaid_balance_sweep_interval_seconds`). Each run re-previews the
whole prepaid cohort and resolves the work item of any account that no longer
has a blocking reason. If other blocking reasons remain, the item stays open
and shows the updated `reason_codes`. Manually resolving an item hides nothing:
the next sweep reopens it while the evidence is still malformed.

After a sanctioned correction:

1. Re-run the diagnostic for the account. The finding must be gone.
2. After the next sweep, confirm that the work item status is `resolved`.
3. The account is now evaluated normally. A service with no current coverage
   is due and goes through standard grace, warning, and enforcement. Tell
   collections if the customer needs to fund the service.

## Never

- Never suspend, restrict, or "unblock" the account by hand because of this
  work item. Never treat ambiguous evidence as debt.
- Never edit invoice periods, invoice lines, adjustments, `origin_ref`, ledger
  entries, or entitlements with SQL, the admin shell, or a one-off script. The
  paid-invoice period repair is the only sanctioned way to set a paid invoice's
  period, and the renewal origin correction is the only sanctioned way to
  rewrite a renewal adjustment's `origin_ref`.
- Never record a period, or acknowledge a warning, that Finance has not
  established from source documents just to clear the quarantine.
- Never infer a period or a subscription from memo, description, or note text,
  or from `next_billing_at`.
- Never void, credit-note, refund, or reverse a charge just to clear the
  quarantine. A money correction needs its own Finance decision and owner.
- Never use the generic adjustment reversal when an entitlement is linked to
  the debit. That leaves orphaned coverage. Use the unused-renewal correction
  instead.
- Never create an entitlement by hand to cover the service.
