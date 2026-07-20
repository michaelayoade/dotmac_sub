# Subscriber Identity Cleanup Audit

**Status:** Approved read-only cleanup slice; no backfill or merge  
**Decision owner:** Michael  
**System of record:** Sub  
**Canonical resolver:** `party.identity_audit`

## Purpose

The 15,291 rows commonly described as "subscribers" are a mixed legacy
population. A row can represent an active service account, former subscriber,
customer awaiting activation, lead, verified contact, test record, or unresolved
placeholder. A table count is therefore not a subscriber count.

This slice produces a repeatable, read-only worklist from native Sub facts. It
does not update `subscribers`, create Parties, quarantine records, merge people,
or call CRM. The output is evidence for the next reviewed backfill slice.

## Separation of concerns

`party.identity_audit` is a resolver and reporting projection. It observes facts
owned by subscriber, sales, access/subscription, provisioning, billing, support,
and verification services. It does not become a second writer for any of them.

The report assigns two independent classifications:

1. **Lifecycle cohort:** how far the row has progressed based on operational
   evidence.
2. **Record classification:** whether the row is a production candidate,
   explicitly non-production, only suspected non-production, or already marked
   for quarantine.

This separation matters. A demo record can have an active subscription; that
does not make it a production subscriber.

## Lifecycle cohorts

The resolver applies the following precedence:

| Cohort | Required native evidence |
| --- | --- |
| `active_subscriber` | At least one active `Subscription` |
| `inactive_subscriber` | Subscription history exists, but none is active |
| `customer` | Confirmed/paid/fulfilled sales order, service order, accepted quote, succeeded/refunded payment, active non-draft billing document, or declared customer/subscriber status |
| `lead` | Lead, quote, unconfirmed sales order, or declared lead status without stronger evidence |
| `verified_contact` | Verified email/channel/current field, successful current NIN verification, or declared contact status |
| `unverified_record` | None of the above |

Support history is reported as context but does not promote lifecycle by itself:
a support ticket can be pre-sales, customer, or internal cleanup traffic.

### Billing enforcement boundary

An account-level billing block is a recoverable access-enforcement state, not a
subscriber lifecycle transition. A row with an active `Subscription` remains in
`active_subscriber` when its account status is `blocked`, `suspended`, or
`delinquent`. `Subscriber.is_active` is likewise an access/portal projection and
is not lifecycle evidence. The audit reports a contradiction only when an active
subscription is attached to a terminal `disabled` or `canceled` account.

This rule assumes billing enforcement changes the account/access projection
while leaving the subscription active. A future change to subscription-state
semantics must update the canonical subscription lifecycle policy first; the
audit consumes that contract rather than inventing a parallel status mapping.

Contradictions are explicit review findings, including an active subscription
on a disabled/canceled account, a lifecycle status lagging stronger commercial
facts, or a `subscriber` status with no subscription history.

## Non-production classification

### Declared non-production

Only explicit metadata is treated as a declaration:

- boolean `is_test`, `test_data`, or `is_demo`; or
- `data_classification`, `environment`, or `record_classification` set to a
  controlled non-production value such as `test`, `demo`, `sandbox`, `qa`,
  `training`, or `staging`.

An explicit quarantine marker is reported separately as `already_quarantined`.

### Suspected non-production

Names containing controlled test tokens and reserved/example email domains are
heuristics. They produce `suspected_nonproduction` and always require manual
review. They never delete, quarantine, or merge a row automatically.

## Duplicate candidate evidence

Duplicate output is grouped evidence, not a merge decision:

| Confidence | Evidence |
| --- | --- |
| High | Same 11-digit current NIN, with a successful verification for each row |
| Medium | Same normalized email and phone; or same normalized name and phone |
| Weak | Same normalized email only; or same normalized phone only |

No confidence level permits automatic merge. Even a verified-NIN match can
represent fraud, a source error, or a correction that needs audit. Shared email,
phone, name, address, reseller ownership, or social handle never proves identity.

Weak groups are retained as review context but do not, by themselves, change a
row's recommended disposition. This prevents a reseller's shared contact email
from sending an entire legitimate customer portfolio into duplicate review.

## Recommended dispositions

The report may recommend:

- ready for a future Party backfill;
- retain an existing quarantine;
- review and quarantine declared non-production data;
- manually review suspected non-production data;
- manually review medium/high duplicate evidence;
- manually review lifecycle contradictions; or
- quarantine an unverified placeholder after review.

These are worklist labels only. Applying any disposition requires a separate
reviewed command with evidence, audit actor, idempotency, and rollback/canonical
redirect policy.

The reviewed decision and dry-run planning contract is defined in
`docs/PARTY_IDENTITY_ADJUDICATION_PLAN.md`. That planner has no apply mode and
does not satisfy the later write-executor gate by itself.

## Operator output and privacy

Run only against a host/database Michael explicitly names:

```bash
python -m scripts.migration.audit_subscriber_identity --out /approved/private/path
```

PostgreSQL runs in one `REPEATABLE READ, READ ONLY` transaction so every
classification is bound to the same live snapshot. The output directory contains:

- `summary.json` — state-bound audit digest, transaction snapshot timestamp,
  missing optional evidence sources, and aggregate cohort, classification,
  disposition, and duplicate counts;
- `subscribers.csv` — subscriber UUIDs, worklist classifications, existing
  Party UUIDs, available controlled display-name sources, and row fingerprints;
- `duplicate_groups.csv` — group IDs, evidence type, confidence, and size; and
- `duplicate_members.csv` — group-to-subscriber UUID membership.

Artifacts intentionally exclude names, email addresses, phone numbers, NINs,
and the normalized evidence keys. Files are created mode `0600`. They still
contain customer identifiers and must remain in an approved private location;
they must not be committed, attached to a PR, or stored in durable memory.

## Cutover gate for the next slice

No Party backfill starts until:

1. the audit runs against the explicitly named target;
2. cohort and duplicate totals are reviewed and signed off;
3. declared/suspected test rules are corrected from real evidence;
4. medium/high duplicate groups have adjudication outcomes;
5. lifecycle contradictions have named owners; and
6. the backfill command proves idempotency, ambiguity quarantine, and zero
   unauthorized merges.

The read-only adjudication planner may be used before gate 6 to prepare and
validate decisions. No resulting plan is executable until a separate reviewed
writer proves that final gate.
