# Customer-subledger opening correction

Use this workflow only when reviewed financial evidence proves that an already
captured customer opening position is wrong. It never edits or deletes the
original opening, and it never creates or duplicates a payment. The owner adds
one append-only correction and the matching customer-position effect in the
same transaction.

The operator must be an active staff user holding
`billing:customer_subledger_opening:correct`. The owner requires exactly that
one permissioned staff principal: it defines **no two-person approval** and no
evidence-file SHA-256 input. The durable review reference is the evidence
binding; Finance's review happens before the operator previews. Use the admin
screen below for routine corrections; the CLI remains for scripted or
break-glass use and calls the same owner.

## Admin screen

Policy context: on 2026-10-08/09 Finance captured 72 openings with "where no
opening balance exists use 0; correct confirmed variances later in the UI".
This screen is that correction path.

1. Open **Billing → Accounts → (account)**. The **Captured opening position**
   panel shows, per currency, the current opening (captured amount plus every
   correction), position time, capture time and actor, evidence reference,
   evidence fingerprint, and the full correction history (applied time,
   before, after, delta, reason, review reference, actor). The panel is hidden
   for accounts without a captured opening.
2. Users holding `billing:customer_subledger_opening:correct` see **Correct
   opening**. It is replaced by the owner's reason when customer-subledger
   authority is not active. Other users see the panel without the action;
   every route also refuses them (403).
3. Enter the corrected amount (signed: positive is customer credit, negative is
   opening debt; at most two decimals), the reason, and the durable Finance
   review reference, then choose **Preview correction**. Nothing is written.
4. The preview page shows the owner's preview: opening before and after, the
   delta, the current and resulting available prepaid balance, the prepaid
   requirement, the enforcement consequence, and the preview fingerprint.
   Consequences are `restoration_eligible` (the account would become funded
   and is restored on the next enforcement run), `suspension_eligible` (it
   would fall below its requirement and may be suspended on the next run),
   `unchanged`, `not_prepaid`, `currency_not_enforced`, or `undetermined`
   (verified funding unavailable — investigate before confirming). The
   correction itself never suspends or restores service.
5. Have Finance verify the before, after, and delta, tick the confirmation, and
   choose **Correct opening to …**. The confirmation is signed, bound to you,
   the account, the currency, and the preview fingerprint, and expires after
   ten minutes.
6. On success you return to the account with the correction identifier and the
   new value at the top of the panel and in its history.

Refusals:

- **The opening position changed after review** (HTTP 409): another
  correction landed after your preview. The page shows a fresh preview from the
  new current value; review it again before confirming.
- **The reviewed account, actor, or preview changed** (409): the confirmation
  was opened by another user, edited, or its values were altered. Preview
  again.
- **Expired or invalid confirmation** (409): preview again.
- **Corrected opening amount already matches the current value** (400): there
  is nothing to correct.
- Missing amount, reason, or review reference, or a reason over 500 / reference
  over 200 characters (400): shown next to the field.

Submitting the same confirmation twice records one correction; the second
submission reports that it was already recorded.

## CLI

Preview first:

```bash
python -m scripts.billing.correct_customer_subledger_opening \
  --account-id ACCOUNT_UUID \
  --currency NGN \
  --corrected-opening-amount EXACT_AMOUNT \
  --reason "REVIEWED_REASON" \
  --review-reference "DURABLE_FINANCE_REFERENCE"
```

Have finance verify the before, after, and delta values. Apply only the exact
reviewed fingerprint:

```bash
python -m scripts.billing.correct_customer_subledger_opening \
  --account-id ACCOUNT_UUID \
  --currency NGN \
  --corrected-opening-amount EXACT_AMOUNT \
  --reason "REVIEWED_REASON" \
  --review-reference "DURABLE_FINANCE_REFERENCE" \
  --apply \
  --expected-preview-fingerprint REVIEWED_SHA256 \
  --command-id NEW_COMMAND_UUID \
  --actor "NAMED_OPERATOR" \
  --actor-system-user-id STAFF_UUID \
  --idempotency-key UNIQUE_OPERATION_KEY
```

The command fails closed if authority is inactive, the opening is absent, the
preview changed, the permission is missing, or the idempotency key conflicts.
Afterward, verify the correction record, posting group, calculated funding
balance, and any separately executed renewal. Do not edit the opening row or
insert another payment as a workaround.
