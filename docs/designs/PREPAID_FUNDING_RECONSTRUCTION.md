# Prepaid funding reconstruction and final authority cutover

**Status:** implementation contract. The authority transition is final.

## Decision

`financial.prepaid_funding_reconstruction` owns the prepaid opening position and
the runtime funding projection. A runtime balance is:

```text
reviewed position at cutover timestamp
+ canonical native financial facts whose occurrence or Sub-recorded time is
  strictly after that timestamp
```

Splynx transactions, subscriber deposit fields, audit tables, exports, and bank
statements are evidence sources. None is a runtime balance source. There is no
configuration switch, shadow reader, or legacy fallback after materialization.

Materialization accepts only one Ed25519-sealed, full-cohort manifest emitted
by the audit exporter after a clean replay. The reconstruction owner verifies
the signature against the config-owned
`billing.prepaid_reconstruction_attestation_public_key_ref`; that setting must
be an OpenBao reference. Unsigned JSON, plaintext/environment trust-key
fallbacks, a non-zero blocker set, changed cohort content, and a second seal for
an already-recorded semantic manifest all fail closed.

The configured `billing.default_currency` supplies the currency unit. Amounts
from different currencies are never minimized, summed, or compared.

Financial facts have two temporal coordinates. Their economic timestamp
(`paid_at`, `issued_at`, or `effective_date`) controls statement ordering and
service chronology; `created_at` records when Sub first knew the fact. A
snapshot includes a fact only when both coordinates are no later than the
snapshot. After materialization, a fact crosses the opening-position boundary
when either coordinate is later. This prevents a late-entered, backdated
payment or adjustment from disappearing behind an already sealed baseline.

## Ownership

- `scripts/one_off/billing_alignment_audit.py` reconstructs observations in an
  isolated audit restore and quarantines incomplete replay.
- `scripts/one_off/export_prepaid_funding_snapshot.py` emits and signs a
  complete-or-error, currency-typed manifest for the exact prepaid cohort. Its
  Ed25519 private key is resolved from an audit-only OpenBao path supplied by
  `--signing-key-ref`; the application must not have read access to that path.
- `financial.prepaid_funding_reconstruction` verifies the sealed manifest and
  its embedded clean blocker manifest, stores the semantic/full-payload/seal/
  blocker/cohort hashes and signer fingerprint with the reviewed batch, stores
  one active baseline per account/currency, and owns later append-only
  supersession.
- Its live reader groups accounts by reviewed baseline timestamp and uses the
  ledger projection's native-only aggregate. Query count follows reviewed
  reconstruction versions, not account count, and the Splynx mirror is never
  queried on this path.
- `customer.financial_position` consumes only the reconstruction owner for
  prepaid funding. Access resolution, plan changes, add-ons, health, and
  enforcement do not reconstruct money.
- The operator dry-run and executable prepaid sweep both resolve funding from
  that live owner. The planner has no file/snapshot injection path that can make
  reviewed money differ from the locked execution decision.
- Canonical payment, credit-note, adjustment, refund, and invoice owners remain
  the only writers of post-cutover financial events.

## Completeness and missing evidence

The first materialization must match the exact non-empty prepaid candidate
cohort. Missing accounts, extra accounts, unknown subscribers, future-dated
positions, duplicate rows, unsupported currency, and unreviewed content hashes
block the whole batch. Partial authority is forbidden.

The funding artifact contains reconstructed available balances only. Required
balance, grace, activation, warning, scheduling, and suspension behavior remain
live config-owned enforcement decisions; they are not copied into this signed
financial fact set.

If replay reports a missing source baseline, paid-through period, payment,
adjustment provenance, service schedule, funded entitlement, or plan decision,
operations must trace the source fact before materialization. Once an authority
opening position exists, every later affordable service-cycle debit must be
owned by an active canonical entitlement for the same subscription and period,
linked to either a paid invoice line or an exact customer-position wallet debit;
reconstruction may detect a missing charge but may not silently replace its
funding evidence. Bank statements may prove receipt of funds, but amount/date
coincidence is not customer attribution. A statement credit must be linked by
reviewed reference or other definitive evidence and posted through the
canonical payment owner. Rerun reconstruction afterward.

Raw bank rows, narrations, customer identity text, account credentials, and
statement files are never stored in the baseline tables. The batch stores only
account IDs, currency, balances, timestamps, hashes, approving actor, source
label, and a non-secret evidence reference.

## Evidence-gap adjudication

The exporter writes a canonical blocker manifest and SHA-256 into
`--blockers-out`. Review decisions must bind that exact hash and cover every
`account_id`/reason pair once. The accepted dispositions are:

- `source_evidence_required`: replace or complete the independently exported
  source evidence, then rerun the replay;
- `canonical_payment_required`: route a definitively attributed, post-handoff
  missing receipt through `financial.payments` preview and confirmation; or
- `quarantine`: keep the account outside materialization.
- `no_paid_through_due_immediately`: only for the exact
  `source_service_without_paid_through_period` blocker, after an authorized
  reviewer confirms against the final source transaction history that the
  service has no charge or any other service-linked period transaction. Exact
  equality with an older reviewed cohort is not proof. Account-level receipts
  do not prove service coverage, and Discount/Correction period rows remain
  separately blocked until their meaning is resolved. The reconstruction
  preserves the source opening balance unchanged and records that the service
  is due immediately; live enforcement still compares that balance with the
  canonical configured requirement.

There is deliberately no generic `resolved` disposition. A no-paid-through
decision must cover the exact hash-bound blocker manifest, may clear no other
reason, and is itself SHA-256-bound into the signed reconstruction source. All
other action plans remain blocked until the owning source is corrected and a
new independent replay no longer emits the blocker.

```json
{
  "schema": "dotmac.prepaid_funding_gap_decisions.v1",
  "blocker_manifest_sha256": "REVIEWED_BLOCKER_SHA256",
  "review_id": "NON_SECRET_FINANCE_CASE_REFERENCE",
  "reviewed_by": "APPROVING_ACTOR",
  "reviewed_at": "2026-07-16T12:00:00Z",
  "decisions": [
    {
      "account_id": "00000000-0000-0000-0000-000000000000",
      "reason": "missing_source_baseline",
      "disposition": "quarantine",
      "evidence_ref": "NON_SECRET_EVIDENCE_REFERENCE"
    }
  ]
}
```

Validate and produce the sanitized owner-action packet:

```bash
python scripts/one_off/adjudicate_prepaid_funding_gaps.py \
  --blockers /approved/prepaid-funding-blockers.json \
  --decisions /approved/prepaid-funding-gap-decisions.json \
  --out /approved/prepaid-funding-gap-actions.json
```

When every action is the reviewed no-paid-through disposition, pass the
sanitized action packet back to the exporter:

```bash
python scripts/one_off/export_prepaid_funding_snapshot.py \
  --snapshot-at REVIEWED_TIMESTAMP \
  --source REVIEWED_SOURCE_LABEL \
  --gap-actions /approved/prepaid-funding-gap-actions.json \
  --out /approved/prepaid-funding-sealed.json \
  --blockers-out /approved/prepaid-funding-blockers.json \
  --signing-key-ref bao://secret/audit/prepaid-reconstruction-signer#private_key_pem
```

The exporter rejects mixed dispositions, stale cohort/blocker hashes, missing
or extra actions, and every unresolved reason. It changes no reconstructed
amount.

## Native prepaid service-cycle renewal

After cutover, `financial.prepaid_service_renewals` owns a due monthly prepaid
period that is funded from the reviewed opening position or later native facts
but is not triggered by a new payment. It locks the account, re-resolves the
canonical customer position, posts one idempotent adjustment debit, links one
active entitlement to that exact debit, and advances the subscription anchor
in the same transaction. An anchor more than two days late is held for reviewed
reconciliation; the scheduled owner never invents historical catch-up charges.

`billing.prepaid_service_renewals` is default-off. When enabled, it suppresses
the older `billing.prepaid_monthly_invoicing` draft-invoice path, including in
dry-run. The two paths are alternative owners for the same service period and
must never run in parallel. Payment-triggered renewals continue through the
payment owner and the same canonical price resolver.

For `canonical_payment_required`, the reviewed row also supplies amount,
currency, timezone-aware `occurred_at`, and
`"definitive_attribution": true`, plus the SHA-256 of the reviewed external
evidence packet. The timestamp must be within the post-handoff replay window.
Amount/date coincidence is rejected. The packet rejects raw bank narration and
other undeclared fields, derives the payment idempotency key from the stable
evidence hash, emits only hashes and non-secret evidence pointers, and never
invokes the payment owner itself.

## Final cutover procedure

1. Generate an Ed25519 keypair through the approved secret workflow. Store the
   private key at an audit-only OpenBao path such as
   `bao://secret/audit/prepaid-reconstruction-signer#private_key_pem`; store the
   public key at
   `bao://secret/billing/prepaid-reconstruction-attestation#public_key_pem` and
   configure that reference in
   `billing.prepaid_reconstruction_attestation_public_key_ref`. Keep the ACLs
   separate; never copy key values into config, files, output, or logs.
2. Restore the approved isolated audit database and run the reconstruction
   exporter with the audit-only key reference:

   ```bash
   python scripts/one_off/export_prepaid_funding_snapshot.py \
     --snapshot-at REVIEWED_TIMESTAMP \
     --source REVIEWED_SOURCE_LABEL \
     --out /approved/prepaid-funding-sealed.json \
     --blockers-out /approved/prepaid-funding-blockers.json \
     --signing-key-ref bao://secret/audit/prepaid-reconstruction-signer#private_key_pem
   ```

   Adjudicate its hash-bound blocker manifest, perform the resulting owner
   actions, and rerun until clean; do not coerce unknown balances to zero.
3. Review the normalized manifest SHA-256, sealed-payload SHA-256, signer
   fingerprint, blocker/cohort hashes, account count, total, currency,
   timestamp, source label, and external evidence packet.
4. Deploy the prepaid funding reconstruction migration after the current
   Alembic head while the old application remains stopped or on its prior
   release.
5. Run the materializer dry-run:

   ```bash
   python scripts/one_off/materialize_prepaid_funding_reconstruction.py \
     --manifest /approved/prepaid-funding-sealed.json
   ```

6. Apply the exact reviewed manifest:

   ```bash
   python scripts/one_off/materialize_prepaid_funding_reconstruction.py \
     --manifest /approved/prepaid-funding-sealed.json \
     --apply \
     --reviewed-sha256 REVIEWED_NORMALIZED_SHA256 \
     --evidence-ref NON_SECRET_FINANCE_REVIEW_REFERENCE \
     --approved-by APPROVING_ACTOR \
     --confirm-final-cutover MATERIALIZE_VERIFIED_PREPAID_FUNDING
   ```

7. Start the new application and verify the full cohort against the reviewed
   positions plus post-baseline native events. The prepaid enforcement control,
   activation timestamp, and zero-day grace remain explicit configuration, but
   cutover adds no initial grace or shadow period: when configured active, the
   owner enforces immediately.

Before step 6, aborting the deployment leaves authority unchanged. Step 6 is
the final authority cutover. The Alembic downgrade refuses to drop the tables
after that record exists. A later error is repaired forward with a newer
reviewed reconstruction batch; it never restores Splynx or subscriber deposit
as authority.

For key rotation, configure the next public-key OpenBao reference before the
audit exporter signs a new supersession. Every accepted batch records the exact
key fingerprint and seal hash. A different seal for an existing semantic
manifest is rejected rather than treated as an idempotent replay.

Accounts created after the final cutover start at a zero opening position and
accumulate canonical native events. A pre-cutover account without a baseline
fails closed. An old postpaid account moving to prepaid therefore needs a
reviewed current baseline as part of that transition.
