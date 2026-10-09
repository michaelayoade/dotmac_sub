# Runbook: prepaid funding audit restore

**Purpose.** Build one exact, complete-history opening artifact for the prepaid
funding cohort so every account has a reviewed baseline and participates in
money-based enforcement.

**Owner.** `app/services/prepaid_funding_reconstruction.py` owns every baseline
write. Nothing in this runbook writes one.

---

## Why a restore is needed at all

`scripts/one_off/export_prepaid_funding_snapshot.py` refuses to run unless

```python
os.getenv("BILLING_AUDIT_EPHEMERAL") == "1"
and current_database().endswith("_audit")
```

That is deliberate. The export replays every financial fact in the system, and
must never do so against the live database. Until this runbook existed there
was no way to satisfy the guard, which made the entire funding-baseline repair
path unreachable in practice.

## What source completion means

Every migrated candidate has complete Splynx transaction history. Credits minus
debits is its source position; a complete empty transaction set is zero. The
isolated resolver then adds canonical Sub-native facts after the fixed handoff.
A dual-reviewed customer proven to be Sub-native before the handoff instead
uses complete canonical Sub facts from account inception; the reviewed decision
does not assign a legacy identity or write money.
A current account without an active baseline is source-batch debt, not an
unknown balance or a permanent customer disposition. Until the exact cohort is
materialized, enforcement continues to fail closed for those accounts. The
completion gate is source-incomplete count zero, opening count equal to the
opening-required cohort, and full-cohort parity. Accounts created after
subledger authority activation start at authoritative zero without a migration
opening.

## Safety model

The audit stack is provisioned beside production, never inside it:

| property | value |
|---|---|
| network | dedicated, `--internal` — no route off the host, none to the app stack |
| ports | none published |
| storage | dedicated volume, destroyed on teardown |
| auth | `trust`, safe because of the two rows above, and avoids minting a credential for a database that lives for minutes |
| live DB | never connected to; the script reads a **dump file** only |

The script refuses to name the live database container, and refuses any
`AUDIT_DB` not ending in `_audit`.

`--allow-primary` is required when running the exporter here, and is correct:
the restore is its own primary. It cannot be abused to reach production,
because the `_audit` database-name check has already rejected that case.

---

## Procedure

Run on the host holding the backups. Prerequisites: a deploy-time dump in
`/var/backups/dotmac_sub/deployments/` or the legacy
`/var/backups/dotmac_sub/` location, and **≥ 60 GB free** — a 2.4 GB gzip
dump expands to roughly 25 GB with indexes. The script preflights both.

### 1. Provision

```bash
scripts/one_off/prepaid_funding_audit_restore.sh provision
```

Uses the newest dump unless given `--dump PATH`. The restore takes a while;
it is verified by counting `subscribers` rows, not by trusting `psql`'s exit
status (a plain-format prod dump reliably emits benign errors for extensions
and roles absent from a bare image). Provisioning waits for the official image's
initialization-complete marker and then for its final PostgreSQL server; an
early `pg_isready` from the temporary bootstrap server is not accepted.

### 2. Export

```bash
scripts/one_off/prepaid_funding_audit_restore.sh export \
  --snapshot-at 2026-07-26T00:00:00+00:00 \
  --source funding-gap-survey-2026-07-26
```

For a bounded, separately reviewed repair, add the exact newline-delimited
account-ID file and acknowledgement to the export command:

```bash
scripts/one_off/prepaid_funding_audit_restore.sh export \
  --snapshot-at 2026-07-26T00:00:00+00:00 \
  --source finance-ticket-REVIEW-ID \
  --reviewed-account-ids-file /out/reviewed-account-ids.txt \
  --confirm-reviewed-scope MATERIALIZE_REVIEWED_PREPAID_SCOPE
```

The export runs as a repository module through `scripts/run_repo_module.sh`, so
the selected image checkout is the import root. It succeeds only when the
selected candidate scope has exact source coverage. A carried account with no
retained source identity is represented as `missing_carried_source_identity` in
the diagnostics and blocks the selected scope. Other missing, duplicate,
malformed, mismatched, or unreconciled history aborts the whole artifact. A
complete empty transaction set is zero. The diagnostics file is written before
a classified blocked exit:

```
/var/backups/dotmac_sub/funding_audit/blockers_<stamp>.json
```

It contains account UUIDs and reason codes only — no customer identity,
credentials, free text, or delivery coordinates.

If the blocker is a genuinely Sub-native pre-handoff account, do not invent a
Splynx ID or edit the restored copy. After the adjudication code is deployed,
run the PII-free preview against the authoritative Sub database:

```bash
python -m scripts.one_off.review_carried_source_identity \
  --account-id ACCOUNT_UUID
```

Only an eligible preview may proceed. Finance and billing must independently
review a content-addressed evidence document. Confirmation requires the exact
fresh fingerprint, evidence reference and SHA-256, two different active staff
UUIDs, attributable actor and reason, and a unique idempotency key:

```bash
python -m scripts.one_off.review_carried_source_identity \
  --account-id ACCOUNT_UUID \
  --apply \
  --confirm RECORD_REVIEWED_NATIVE_BEFORE_HANDOFF \
  --fingerprint PREVIEW_SHA256 \
  --evidence-ref APPROVED_EVIDENCE_POINTER \
  --evidence-sha256 EVIDENCE_SHA256 \
  --reviewed-by-id BILLING_STAFF_UUID \
  --approved-by-id FINANCE_STAFF_UUID \
  --actor OPERATOR_PRINCIPAL \
  --reason REVIEWED_REASON \
  --idempotency-key UNIQUE_BUSINESS_KEY
```

This writes only the immutable adjudication, audit evidence, and owner output.
Take a new database dump after confirmation, destroy and reprovision the audit
restore from that dump, and rerun the complete export. The resolver rechecks
the stored fingerprint; newly discovered Splynx evidence or changed provenance
invalidates the decision and blocks export.

The default export is still full-cohort. A bounded reconciliation may be
requested only with a reviewer-owned newline-delimited account-ID file and the
explicit acknowledgement `MATERIALIZE_REVIEWED_PREPAID_SCOPE` on both export
and materialization. The selected accounts must be a subset of the current
prepaid candidate cohort; the signed manifest and materializer still enforce an
exact match to that selected scope. This is appropriate for a separately
approved repair ticket, and does not permit arbitrary accounts or an unsigned
partial artifact.

### 3. Destroy

```bash
scripts/one_off/prepaid_funding_audit_restore.sh destroy
```

Removes the container, the volume (and with it the restored copy of production
data), and the network. Exported artifacts are kept.

**Do not leave the audit stack running** — it is a full copy of production.

---

## After the export: the completion pipeline

1. `export_prepaid_funding_snapshot.py` — resolves the complete frozen history
   plus canonical Sub-native facts in the lawful interval for each provenance
   disposition and emits a signed exact-cohort artifact.
2. **Correct the source snapshot, then rerun.** An integrity error clears only
   when the resolver proves the complete source; never edit a generated target.
3. `materialize_prepaid_funding_reconstruction.py` — verifies the Ed25519
   signature against the public key at billing setting
   `prepaid_reconstruction_attestation_public_key_ref`, then applies. Dry-run
   by default; apply requires `--reviewed-sha256`, `--evidence-ref`,
   `--approved-by`, and `--confirm-final-cutover`.

### Three things that surprise people

**Manifests are exact-scope, full-cohort by default.** Without a reviewed scope
file, the materializer passes
`expected_account_ids = candidate_prepaid_funding_account_ids(db)` — the whole
cohort — and preview blocks on any `missing_reconstruction_account` or
`unexpected_reconstruction_account`. With a confirmed reviewed scope file, the
same exact-match checks apply to that selected candidate subset. A signed
manifest with any blocker/excluded account is rejected.

The only account-scoped exception is not a reconstruction manifest at all.
`docs/runbooks/NATIVE_PREPAID_OPENING_REPAIR.md` covers a Sub-native account
created after the legacy handoff that was accidentally omitted despite
existing at the original cutover. That owner independently proves zero Splynx
evidence and fingerprints canonical Sub facts at the sealed cutover. It cannot
repair a migrated/Splynx-linked account, alter a batch, or supply a cohort
hash, so the complete-cohort rule above remains unchanged.

**A repair batch is not the cutover.** The authority cutover already exists, so
a new batch gets `is_authority_cutover = False`, and its `position_at` must be
strictly newer than the existing baselines or preview blocks with
`reconstruction_position_not_newer`.

**The target is not a copied deposit field.** For a migrated account, the
resolver proves the complete active transaction net equals the final source
position, then adds canonical post-handoff Sub facts. A reviewed pre-handoff
Sub-native account has no Splynx history component and uses complete canonical
Sub facts from inception. A mismatch or stale adjudication blocks the whole
artifact.

## Activation guard: a legacy account about to enter the quarantine

Prepaid activation is refused for an account that existed when
customer-subledger authority activated and has neither an active reviewed
baseline nor a subledger opening
(`financial.prepaid_activation_funding_guard`, see
`docs/FINANCIAL_ACCESS_ENFORCEMENT.md` "Prepaid activation funding admission").
Creating a prepaid subscription (pending or active), activating a pending one,
moving a subscription or account to prepaid, and bulk provisioning all fail
with `...funding_quarantined` and a message naming the runbook. The admin
customer and subscription pages show a **Prepaid funding quarantine** banner
with the same reason and runbook.

### Clear the quarantine (the normal path)

Read the reason on the banner or refusal (example: account 25448, created
2025-03-03 and given its first prepaid subscription on 2026-10-07):

| Reason | Do this |
| --- | --- |
| `migrated_opening_missing` (Splynx-linked) | `docs/runbooks/REVIEWED_MIGRATED_PREPAID_OPENING_REPAIR.md`: no-write preview, operator and Finance approval, capture the reviewed opening at the original cutoff. |
| `carried_source_identity_unresolved` (pre-handoff, no Splynx identity) | Section 2 above: `review_carried_source_identity` preview and dual review, then a bounded export/materialization with a reviewed scope file and `MATERIALIZE_REVIEWED_PREPAID_SCOPE`. Never invent a Splynx ID. |
| `reviewed_native_opening_missing` (adjudication recorded) | Rerun the bounded export/materialization for that account from a fresh restore. |
| `native_after_handoff_opening_missing` | `docs/runbooks/NATIVE_PREPAID_OPENING_REPAIR.md` (or `CUSTOMER_SUBLEDGER_CUTOVER.md` §3a). |
| `source_identity_unclassifiable` | Stale or conflicting adjudication/identity evidence: re-review the carried-source identity first. |

When the baseline or opening exists the banner disappears, activation is
admitted without an override, and the account leaves
`billing_prepaid_funding_quarantined_accounts`. Revoke any override left on
the account (banner → **Revoke override**, reason required) so the record shows
it is no longer relied on.

### Override (only when service must start before the review completes)

A user holding `billing:prepaid_funding:activation_override` (seeded for
`admin` only; grant it to another role deliberately) opens the customer page,
expands **Record activation override** in the banner, gives a reason of at
least ten characters (ticket, customer impact, who is doing the review) and
confirms. This writes one `prepaid_activation_funding_overrides` row with the
real staff user, an audit event, and an owner event; every later activation it
admits writes `prepaid_activation_admitted_by_funding_override`. Then retry the
original create/activation.

The override is not funding. The account stays excluded from prepaid
warning/suspension/restoration, stays in the quarantine count, and
`SubPrepaidFundingQuarantineGrowing` still fires for it. Start the clearing
path above in the same ticket; the override does not expire on its own.

Audit trail:

```sql
SELECT action, actor_id, occurred_at, metadata
  FROM audit_events
 WHERE entity_type = 'subscriber'
   AND entity_id = 'ACCOUNT_UUID'
   AND action LIKE 'prepaid_activation%'
 ORDER BY occurred_at;
```

## Monitoring

`billing_prepaid_funding_quarantined_accounts` is the retained compatibility
metric for the source-incomplete count. It must reach zero at completion and
remain zero. Any later non-zero value is a source/materialization regression,
not a permitted steady state. While legacy review stock remains, track its
absolute level as remediation work and alert any 24-hour increase separately as
the prevention signal. A constant legacy cohort must not create a permanent
warning that operators learn to ignore.

With the activation guard deployed, an increase has three explanations, in
order of likelihood: a recorded activation override (check the
`prepaid_activation_admitted_by_funding_override` audit rows), an existing
disabled prepaid service resumed or an account re-approved for billing (paths
the guard does not cover), or a guard regression. Each is cleared by capturing
the account's opening as above.
