# Party Identity Adjudication and Backfill Plan

**Status:** Approved planner; guarded executor implemented but no production execution  
**Decision owner:** Michael  
**System of record:** Sub  
**Canonical planner:** `party.identity_adjudication`

## Purpose and boundary

The subscriber identity audit produces evidence and review recommendations. It
does not authorize a Party write. This slice turns explicit human decisions
into a deterministic dry-run plan while keeping `party.registry` as the future
writer.

The planner is deliberately incapable of changing the database. It cannot:

- create, activate, quarantine, merge, or archive a Party;
- populate or repoint `Subscriber.party_id`;
- copy names or contact points;
- assign customer, subscriber, reseller, vendor, partner, or other roles;
- change account, subscription, billing, access, or authentication state; or
- call CRM.

There is no `--apply` option.

## Review flow

```text
native Sub facts
      │
      ▼
party.identity_audit ──> audit digest + row fingerprints
      │
      ▼
private blank decision template
      │ reviewed in protected storage
      ▼
party.identity_adjudication
      │
      ▼
PII-free Party/binding plan requiring separate expiring approval
```

Generate a private template only against a host/database Michael explicitly
names:

```bash
python -m scripts.migration.plan_subscriber_party_backfill \
  --template --out /approved/private/party-review
```

After reviewers complete selected rows, validate them against a fresh native
audit:

```bash
python -m scripts.migration.plan_subscriber_party_backfill \
  --decisions /approved/private/party-review/party_identity_decisions.csv \
  --out /approved/private/party-plan
```

PostgreSQL facts are read in one `REPEATABLE READ, READ ONLY` transaction. A
blank `action` remains unreviewed and does not enter the plan. `defer` is an
explicit reviewed decision but produces no Party or binding action.

## Digest and drift contract

The audit digest covers every row fingerprint, duplicate group, evidence level,
existing Party binding, available display-name source, and optional evidence
source. It excludes transaction time, so a later run with identical facts has
the same digest.

Every reviewed row carries both the audit digest and its row fingerprint. The
planner refuses stale decisions when any covered fact changes. It also refuses
duplicate decision IDs, more than one decision for a Subscriber, unknown
Subscribers, future/naive review timestamps, blank reviewer/reason evidence,
and actionable rows that already have a Party binding.

## Explicit Party decisions

Supported actions are:

| Action | Result in the dry-run plan |
| --- | --- |
| `create_active_party` | Plan a production Party with active identity state |
| `create_quarantined_party` | Plan a Party that remains quarantined pending resolution |
| `defer` | Record review without planning a Party or binding |

Every creation decision explicitly supplies:

- a new `planned_party_id` UUID;
- Person or Organization `party_type`;
- Party data classification;
- the Subscriber whose native field supplies the display name;
- the controlled display-name source;
- reviewer identity, timezone-aware review time, and reason.

The planner never infers Person versus Organization from `company_name`.
Person Parties may use subscriber full/display name. Organization Parties may
use company, legal, or subscriber display name, and the selected field must
actually be present in the current audit facts.

One Party may own several service accounts. Reviewers express that decision by
assigning those accounts the same `planned_party_id` and the same Party
contract. This is pre-creation grouping, not an automatic merge of existing
Parties.

## Duplicate closure

Weak shared-contact groups remain context only. For medium or high duplicate
evidence, the planner refuses partial action: once any member is actionable,
every member of that evidence group must have an actionable reviewed decision.
Reviewers may resolve the members to one planned Party or explicitly distinct
planned Party UUIDs. Omission and `defer` cannot silently create one side of a
possible duplicate.

No evidence level sets `automatic_merge_allowed=true`.

## Privacy and artifacts

The decision CSV is protected input and may contain reviewer reason text. It
must be mode `0600`; group/world-readable input is refused. Output directories
are mode `0700` and files are mode `0600`.

The generated plan contains only UUIDs, controlled classifications, digests,
counts, and hashes of reviewer/reason text. It excludes names, email, phone,
NIN, display names, reason text, and normalized duplicate keys. Artifacts must
not be committed, attached to a pull request, or stored in durable memory.

## Executor gate

The guarded executor is implemented as `party.identity_backfill_executor`; its
full operating contract is `docs/PARTY_IDENTITY_BACKFILL_EXECUTION.md`. A plan
alone remains non-executable. Execution additionally requires the exact private
decision file, exact generated plan file, an expiring separate approval bound
to both file hashes and both digests, exact write-count limits, typed digest
confirmation, and explicit execution acknowledgement.

The executor re-reads the complete audit, locks the selected Subscriber rows,
refuses drift without an override, creates predetermined Party UUIDs through
`party.registry`, binds accounts through `party.bind_subscriber_account`, and
records a PII-free receipt manifest. It never merges, repoints, assigns a role,
copies contact points, or changes lifecycle, billing, access, or authorization
state. No implementation or migration authorizes a production run.
