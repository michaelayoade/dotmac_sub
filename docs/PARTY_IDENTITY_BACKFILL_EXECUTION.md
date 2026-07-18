# Party Identity Backfill Execution

**Status:** Guarded executor implemented locally; no production execution  
**Decision owner:** Michael  
**System of record:** Sub  
**Execution gate:** `party.identity_backfill_executor`  
**Record writer:** `party.registry`

## Boundary

The executor applies only a fresh, reviewed Subscriber-to-Party plan whose
facts and files are unchanged. It is an adapter around the canonical Party
commands, not a second identity owner. The service never commits; the operator
command owns one outer transaction.

The executor may perform exactly two business writes:

1. create a predetermined active or quarantined Person/Organization Party; and
2. bind the explicitly reviewed Subscriber accounts to that Party through
   `party.bind_subscriber_account`.

It cannot merge or repoint an identity, assign any role or permission, copy a
contact point, activate a Subscriber, or change subscription, billing, access,
authentication, reseller, vendor, partner, sales, or support state.

## Required evidence

Execution requires three private mode-`0600`, non-symlink files:

- the exact reviewed `party_identity_decisions.csv`;
- the generated `party_backfill_plan.json`; and
- a separately prepared approval envelope.

The approval envelope is never committed or stored in durable memory. Its
shape is:

```json
{
  "contract_version": 1,
  "plan_digest": "<64 lowercase hex characters>",
  "audit_digest": "<64 lowercase hex characters>",
  "decision_file_sha256": "<64 lowercase hex characters>",
  "plan_file_sha256": "<64 lowercase hex characters>",
  "approved_by": "<reviewed operator identity>",
  "approved_at": "<timezone-aware ISO-8601 timestamp>",
  "expires_at": "<timezone-aware ISO-8601 timestamp>",
  "reason": "<protected approval reason>",
  "maximum_parties": 0,
  "maximum_bindings": 0
}
```

The maxima must exactly equal the generated plan counts, and the approval time
must follow plan generation. An approval window cannot exceed 24 hours. One
execution is capped at 500 Parties and 1,000 bindings with no command-line
override; larger work is re-audited and reviewed in separate batches. Inputs are hashed
before parsing and checked again before database access; a file that changes
during validation is refused. The approval must be
unexpired, its plan and decision file hashes must match the files byte for
byte, and its audit/plan digests must match a newly resolved native audit and
plan. There is no force, stale-data, skip-lock, merge, or repoint override.

## Transaction and idempotency

On PostgreSQL, the operator command starts one `SERIALIZABLE, READ WRITE`
transaction before collecting the current audit. The service then:

1. validates the approval and file hashes;
2. rebuilds the complete audit and plan;
3. compares the exact audit and plan digests and approved count limits;
4. locks every selected Subscriber row;
5. refuses any pre-existing planned Party UUID or bound Subscriber;
6. creates Parties with their predetermined UUIDs through `party.registry`;
7. binds each account through `party.bind_subscriber_account`; and
8. inserts one PII-free `party_identity_backfill_receipts` row.

The Party creations, bindings, and receipt commit together. A failure rolls the
whole transaction back. The service itself calls neither `commit()` nor an
external system.

The receipt's unique `plan_digest` and canonical manifest provide exact retry
evidence. An exact retry creates nothing and verifies that every planned Party,
binding target, binding source, binding reason hash, and Party receipt marker
still agrees. Missing or changed evidence is refused; the executor never
silently recreates, repairs, merges, or repoints identity state.

## Receipt and compensation evidence

Migration 351 adds `party_identity_backfill_receipts`. A receipt contains only
the exact approval-file hash, plan/input digests, approval text hashes,
timestamps, counts, and the PII-free plan manifest.
Raw approver identity, approval reason, reviewer reason, display names, email,
phone, NIN, and duplicate keys are not stored in the receipt.

Each Subscriber binding records the full plan digest as its source plus the
decision UUID and reason hash. Each created Party records the plan/audit
digests, selected identity-source Subscriber UUID, controlled display source,
Subscriber UUIDs, and decision UUIDs. These facts identify the exact affected
rows for a later reviewed compensation or repoint workflow.

Transaction rollback is the only automatic rollback. After commit, identity
changes are business facts: there is no delete/unbind shortcut. A correction
requires the future reviewed merge/repoint owner and domain reconcilers.

## Operator invocation

The command remains unusable without both typed confirmation and an explicit
execution acknowledgement:

```bash
python -m scripts.migration.execute_subscriber_party_backfill \
  --decisions /approved/private/party-review/party_identity_decisions.csv \
  --plan /approved/private/party-plan/party_backfill_plan.json \
  --approval /approved/private/party-plan/execution_approval.json \
  --confirm-plan-digest '<exact plan digest>' \
  --execute
```

This checked-in command does not authorize deployment, migration, or execution
against any environment. Michael must separately name the production target
and explicitly authorize the production operation after reviewing the fresh
counts and digests.
