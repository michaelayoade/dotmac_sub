# Rejected Deposit Intent Repair

Use this runbook when a rejected direct-bank-transfer receipt still leaves a
customer's Deposit Account Credit request displayed as under review.

## Safety contract

- Production execution requires Michael to name the target host.
- The command is read-only unless `--apply` is supplied.
- Apply requires an unlimited fresh dry-run fingerprint, named target,
  attributable actor, and operator reason.
- Only submitted Deposit Account Credit intents linked to an exact rejected
  payment proof are eligible.
- Account, reference, amount, currency, intent ID, and proof ID must all match.
- Missing or conflicting evidence is reported and not changed.
- Re-running the same approved fingerprint is idempotent.

## Dry run

Run from the application release directory:

```bash
poetry run python -m scripts.one_off.repair_rejected_deposit_intents
```

Review `classification_counts` and every candidate. Expected repair rows are
`eligible`. Investigate `missing_proof_link`, `proof_not_found`,
`proof_not_rejected`, or `evidence_mismatch` separately; do not bypass their
classification.

Use `--summary-only` when operator output must omit record identifiers. A
positive `--limit` is available for initial read-only inspection but cannot be
used for apply.

## Apply

After reviewing a fresh unlimited dry run:

```bash
poetry run python -m scripts.one_off.repair_rejected_deposit_intents \
  --apply \
  --confirm-fingerprint <sha256-from-dry-run> \
  --target <Michael-named-host> \
  --actor-id <operator-identity> \
  --reason "<incident/change reference and purpose>"
```

The apply command aborts if the fingerprint changed or any selected source
evidence drifted. It records one item audit per requested intent and one batch
audit keyed by the preview fingerprint.

## Verification

1. Run the dry run again; repaired pairs must no longer be eligible.
2. Confirm the batch reports the expected `applied_count`.
3. Open an affected customer portal and confirm the under-review blocker is
   gone, the rejection message is visible, and a new deposit can be started.
4. Confirm the intent has status `rejected`, retains its original
   `payment_proof_id`, and records rejection source/time metadata.
5. Review `topup_intent.direct_transfer_rejected` event and the item/batch audit
   evidence.

## Recovery policy

Do not manually change the intent back to `submitted`. A genuine payment found
after rejection must enter the canonical verification/settlement path; terminal
intent recovery remains observable and idempotent.
