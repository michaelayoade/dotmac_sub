# Account-scoped Sub-native prepaid opening repair

Owner: `financial.customer_subledger_opening_positions`

Permission: `billing:prepaid_funding:native_opening_repair`

Use this command only for a prepaid account created after the legacy financial
handoff that existed at the original prepaid-funding authority cutover but was
omitted from both the active funding baseline and customer-subledger opening.
The command is dry-run-first and never accepts an operator-entered balance.

Do not use it for a migrated or Splynx-linked account. Any Splynx customer ID
or transaction, pre-handoff creation, post-cutover creation, missing cohort
membership, active baseline, existing opening, or incomplete canonical Sub
history is a terminal eligibility failure. Correct source evidence through its
own owner; never alter the sealed reconstruction batch or invent a cohort hash.

## Preview

Obtain a Finance-approved, content-addressed evidence document. Its reference
must not contain credentials or secret query parameters. Then run:

```bash
python -m scripts.billing.repair_native_prepaid_opening \
  --account-id ACCOUNT_UUID \
  --currency NGN \
  --finance-approver-system-user-id FINANCE_SYSTEM_USER_UUID \
  --finance-approver-name "CANONICAL_FINANCE_NAME" \
  --approved-at ISO_8601_TIMESTAMP_WITH_OFFSET \
  --ticket-reference TICKET_REFERENCE \
  --evidence-ref NON_SECRET_EVIDENCE_REFERENCE \
  --evidence-sha256 LOWERCASE_SHA256
```

Retain the JSON. Verify the account and currency, `native_after_handoff`
classification, zero Splynx transaction count, original authority batch/time,
calculated amount, native and shadow evidence fingerprints, opening residual,
cutover and source-identity fingerprints, and final preview fingerprint. Stop
if any field disagrees with Finance's
evidence.

## Apply

Repeat every preview argument and add:

```bash
  --apply \
  --fingerprint EXACT_PREVIEW_FINGERPRINT \
  --operator-system-user-id AUTHORIZED_OPERATOR_SYSTEM_USER_UUID \
  --reason "Finance-approved repair of omitted Sub-native prepaid opening" \
  --idempotency-key STABLE_TICKET_ACCOUNT_KEY
```

Apply rechecks the operator's live permission, Finance identity and evidence,
account/cohort/source classification, original cutover, native facts, Splynx
absence, and competing baseline/opening after locks. A stale preview makes no
changes. Exact replay with the same key and fingerprint returns the committed
repair without writing another row or event.

## Verify

After an explicit apply, confirm:

1. One append-only `native_prepaid_opening_repairs` row carries the reviewed
   cutover, fingerprints, approval, operator, reason, and idempotency evidence.
2. One `customer_subledger_opening_positions` row references that repair and no
   verification run, with `legacy_position` equal to the calculated amount.
3. Its one posting group contains only the exact residual needed after shadow
   facts known at the original cutover.
4. One audit event and one `customer_subledger.native_opening_repaired` owner
   event share the repair/opening identities and preview fingerprint.
5. `verified_prepaid_funding_balance` equals the approved opening plus canonical
   native facts after the original cutover.
6. No invoice, Payment, ledger entry, subscription, entitlement, access state,
   or `next_billing_at` changed.
7. Repeating apply with the exact same key reports `replayed: true` and creates
   no duplicate evidence.

The command repairs authority evidence only. Run any later invoice settlement
as a separate previewed owner command.
