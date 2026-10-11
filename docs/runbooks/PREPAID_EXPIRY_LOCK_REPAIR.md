# Prepaid lock recovery after wrongful expiry

This is a reviewed repair for a suspended prepaid subscription whose exact
prepaid enforcement lock was resolved by the expiry job. It does not activate
service, settle a payment, grant coverage, change a billing anchor, or remove
other holds. First deploy the nonterminal prepaid-expiry guard and confirm that
the scheduled expiry task can no longer repeat the incident.

The operator selects both the current subscription UUID and its historical
inactive `EnforcementLock` UUID. The preview requires that historical lock to
have reason `prepaid`, `resolved_by=expired`, and a resolution time no later than
the stated `as_of`; the service must still be suspended and prepaid, its fixed
contract must not have ended, and the due renewal must have no current funded
entitlement or applied grant. Covered or unresolved service is blocked. An
existing active prepaid lock is left alone. Other active lock reasons are
preserved and never interpreted as prepaid authority.

First discover the review cohort within the known expiry-run window. The
command is read-only and returns exact subscription/lock pairs plus blockers;
repeat with reviewed narrower windows or batches as needed:

```bash
python -m scripts.one_off.discover_prepaid_expiry_lock_repairs \
  --resolved-since INCIDENT_WINDOW_START_UTC \
  --resolved-before INCIDENT_WINDOW_END_UTC \
  --limit 100 --offset 0
```

Advance `--offset` by the previous batch size to review the remaining rows.
The ordering is deterministic by subscription and historical-lock identity.

On a development or authorized operations host, preview one exact service:

```bash
python -m scripts.one_off.repair_prepaid_expiry_locks \
  --subscription-id SUBSCRIPTION_UUID \
  --historical-lock-id RESOLVED_PREPAID_LOCK_UUID \
  --as-of CURRENT_UTC_INSTANT
```

Review the account's payment, coverage, service and lock history. Confirm the
fingerprint and exact IDs, then apply only that reviewed preview:

```bash
python -m scripts.one_off.repair_prepaid_expiry_locks \
  --subscription-id SUBSCRIPTION_UUID \
  --historical-lock-id RESOLVED_PREPAID_LOCK_UUID \
  --as-of SAME_CURRENT_UTC_INSTANT \
  --apply --fingerprint REVIEWED_SHA256 \
  --idempotency-key STABLE_REPAIR_KEY \
  --actor APPROVING_OPERATOR \
  --reason "Reviewed expiry-cleared prepaid lock"
```

The command rechecks under account, subscription and historical-lock row locks.
The as-of instant must be no more than 15 minutes old. Changed evidence fails
closed; take a new preview. Repeating a successful
command returns the same new active lock. Process small canaries, then verify
the subscription remains suspended, the prepaid lock exists, unrelated holds
remain active, and a later confirmed funding event is eligible for the normal
payment-triggered renewal. Verify the paid invoice, entitlement, billing anchor,
prepaid lock restoration, and RADIUS projection after that payment. This repair
does not itself trigger renewal or network restoration.

If confirmed funding was already present before the lock was repaired, do not
wait for another payment or invent one. Review the exact contracted period,
tax-inclusive amount, payment-backed credit and any existing entitlement;
then use the existing reviewed renewal owner with the explicitly limited
`funding_recovery` eligibility context:

```bash
python -m scripts.billing.billing_target_shadow preview-prepaid-service-renewal \
  --subscription SUBSCRIPTION_UUID --starts-at PERIOD_START_UTC \
  --ends-at PERIOD_END_UTC --amount EXACT_CONTRACT_CHARGE \
  --eligibility-context funding_recovery

python -m scripts.billing.billing_target_shadow execute-reviewed-prepaid-service-renewal \
  --subscription SUBSCRIPTION_UUID --starts-at PERIOD_START_UTC \
  --ends-at PERIOD_END_UTC --amount EXACT_CONTRACT_CHARGE \
  --eligibility-context funding_recovery \
  --preview-fingerprint REVIEWED_RENEWAL_SHA256 \
  --evidence-ref FINANCE_APPROVAL_REFERENCE \
  --actor APPROVING_OPERATOR --idempotency-key STABLE_RENEWAL_KEY
```

The renewal owner, not this lock repair, settles the exact invoice, creates
coverage, advances the billing anchor and requests prepaid-only restoration.
An independent admin or SLA hold remains active. If the renewal preview is not
allowed, resolve its funding/coverage blocker through the named finance owner;
do not weaken the eligibility gate.

Services without a provable historical prepaid lock, or with former admin/SLA
holds, must not be bulk-assigned a prepaid reason. Route those to a separate
review under the owning financial and access policies; preserve their current
suspended state meanwhile. The repair is not a substitute for funding evidence
or for correcting a terminal fixed-term contract.
