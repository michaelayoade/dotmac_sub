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

As of this runbook, **no reviewed owner can correct this record**. Work out
which case applies, record it, and escalate to engineering. Leave the account
quarantined until then. It stays protected while quarantined.

1. **A line is not a service charge.** The diagnostic shows a line `kind` other
   than `base_subscription`. Finance confirms from source documents that the
   line was a one-off charge, such as installation, equipment, or a fee. Route:
   `engineering_non_service_line_classification`. The missing capability is a
   reviewed way to mark a paid, subscription-linked line as "not a service
   period".
2. **The period is proven by structured data.** `period_proof` is
   `source_entitlement` (exactly one active entitlement was created from each
   line) or `line_metadata_period` (the line's structured
   `billing_period_start`/`billing_period_end`). Finance confirms
   `proven_period_start`..`proven_period_end` is the period the payment bought.
   Route: `engineering_paid_invoice_period_restoration`. Escalate with the
   proposed values.
3. **The period is not proven.** `period_proof` is `none`, `conflicting`, or
   `derived_line_metadata_period`. A derived period was inferred from the
   payment date, so it is not proof. Finance determines the paid period from
   source documents: the original or Splynx invoice, the payment receipt, and
   the customer order. Memo text is never a source. Route:
   `engineering_documentary_paid_invoice_period`.

These existing tools **do not** fix this code. Do not run them hoping they
will:

- the admin invoice "prepaid coverage reconciliation" screen and
  `scripts/billing/prepaid_coverage_reconcile.py`, which only create
  entitlements from an exact period;
- `reconcile_prepaid_drafts --repair-paid-invoice`, which only repairs an
  unlinked line;
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
   - **Service was received.** There is no sanctioned repair, because the debit
     is legitimate and only its reference is wrong. Route:
     `engineering_renewal_origin_correction`. The linked entitlement proves the
     correct reference, which the diagnostic shows.
3. **No entitlement is linked to the debit.**
   - **Finance confirms the debit was raised in error** (it bought no service
     period). This route is sanctioned: a reviewed account-adjustment reversal
     (owner `financial.account_adjustments`, permission
     `billing:ledger:write`). Call
     `POST /api/v1/account-adjustments/<adjustment-id>/reversal/preview` with
     the approval reference as `reason`. Confirm with `POST .../reversal`,
     passing the exact `preview_fingerprint` and a unique `idempotency_key`.
   - **Otherwise** there is no sanctioned repair. Route:
     `engineering_renewal_origin_correction`.
4. **Any other linked shape** (several entitlements, an invoice-linked
   entitlement, or an amount or currency mismatch) has no sanctioned repair.
   Escalate it.

Every sanctioned route returns the debit to the customer's prepaid funding.
Use one only when Finance has decided the charge itself was wrong, never just
to clear the quarantine.

## 3. Approvals and evidence

Each correction has two roles:

- a **Finance approver**, who decides the fact in the option's `when` from
  source documents; and
- a separate **operator**, who runs the preview and apply.

The two must be different people. The existing owners record one actor. The
approver is recorded through the approval reference in `--reason`/`reason` and
in the finance ticket.

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
  entries, or entitlements with SQL, the admin shell, or a one-off script.
- Never infer a period or a subscription from memo, description, or note text,
  or from `next_billing_at`.
- Never void, credit-note, refund, or reverse a charge just to clear the
  quarantine. A money correction needs its own Finance decision and owner.
- Never use the generic adjustment reversal when an entitlement is linked to
  the debit. That leaves orphaned coverage. Use the unused-renewal correction
  instead.
- Never create an entitlement by hand to cover the service.
