# Prepaid funding reconstruction and final authority cutover

**Status:** implementation contract. The authority transition is final.

## Decision

`financial.prepaid_funding_reconstruction` owns the prepaid opening position and
the runtime funding projection. A runtime balance is:

```text
reviewed position at cutover timestamp
+ canonical native financial events strictly after that timestamp
```

Splynx transactions, subscriber deposit fields, audit tables, exports, and bank
statements are evidence sources. None is a runtime balance source. There is no
configuration switch, shadow reader, or legacy fallback after materialization.

The configured `billing.default_currency` supplies the currency unit. Amounts
from different currencies are never minimized, summed, or compared.

## Ownership

- `scripts/one_off/billing_alignment_audit.py` reconstructs observations in an
  isolated audit restore and quarantines incomplete replay.
- `scripts/one_off/export_prepaid_funding_snapshot.py` emits a complete-or-error,
  currency-typed manifest for the exact prepaid cohort.
- `financial.prepaid_funding_reconstruction` verifies the normalized manifest,
  stores the reviewed batch and one active baseline per account/currency, and
  owns later append-only supersession.
- `customer.financial_position` consumes only the reconstruction owner for
  prepaid funding. Access resolution, plan changes, add-ons, health, and
  enforcement do not reconstruct money.
- Canonical payment, credit-note, adjustment, refund, and invoice owners remain
  the only writers of post-cutover financial events.

## Completeness and missing evidence

The first materialization must match the exact non-empty prepaid candidate
cohort. Missing accounts, extra accounts, unknown subscribers, future-dated
positions, duplicate rows, unsupported currency, and unreviewed content hashes
block the whole batch. Partial authority is forbidden.

If replay reports a missing source baseline, paid-through period, payment,
adjustment provenance, service schedule, or plan decision, operations must
trace the source fact before materialization. Bank statements may prove receipt
of funds, but amount/date coincidence is not customer attribution. A statement
credit must be linked by reviewed reference or other definitive evidence and
posted through the canonical payment owner. Rerun reconstruction afterward.

Raw bank rows, narrations, customer identity text, account credentials, and
statement files are never stored in the baseline tables. The batch stores only
account IDs, currency, balances, timestamps, hashes, approving actor, source
label, and a non-secret evidence reference.

## Final cutover procedure

1. Keep prepaid enforcement disabled.
2. Restore the approved isolated audit database and run the reconstruction
   exporter. Resolve every blocker; do not coerce unknown balances to zero.
3. Review the normalized SHA-256, account count, total, currency, timestamp,
   source label, and external evidence packet.
4. Deploy migration `320_prepaid_funding_reconstruction` while the old
   application remains stopped or on its prior release.
5. Run the materializer dry-run:

   ```bash
   python scripts/one_off/materialize_prepaid_funding_reconstruction.py \
     --manifest /approved/prepaid-funding.json
   ```

6. Apply the exact reviewed manifest:

   ```bash
   python scripts/one_off/materialize_prepaid_funding_reconstruction.py \
     --manifest /approved/prepaid-funding.json \
     --apply \
     --reviewed-sha256 REVIEWED_NORMALIZED_SHA256 \
     --evidence-ref NON_SECRET_FINANCE_REVIEW_REFERENCE \
     --approved-by APPROVING_ACTOR \
     --confirm-final-cutover MATERIALIZE_VERIFIED_PREPAID_FUNDING
   ```

7. Start the new application and verify the full cohort against the reviewed
   positions plus post-baseline native events before enabling enforcement.

Before step 6, aborting the deployment leaves authority unchanged. Step 6 is
the final authority cutover. The Alembic downgrade refuses to drop the tables
after that record exists. A later error is repaired forward with a newer
reviewed reconstruction batch; it never restores Splynx or subscriber deposit
as authority.

Accounts created after the final cutover start at a zero opening position and
accumulate canonical native events. A pre-cutover account without a baseline
fails closed. An old postpaid account moving to prepaid therefore needs a
reviewed current baseline as part of that transition.
