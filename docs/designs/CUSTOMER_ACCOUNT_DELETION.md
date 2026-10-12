# Customer account hard deletion

Status: customer hard-deletion owner migration; hosted acceptance required.

Accepted characterization: #3576 main
`a61237f38abcdf0e1975211a3fda4cee133a0939`, tree
`dbd482b16636bb9505d0c5ada6f32cb77de4e24f`. All eighteen cases passed in
hosted CI `38255912236`, PostgreSQL shard 2 job `114834251602`.
Knowledge reference: `dotmac-sub-security-implementation-2026-10-11` version 46.

This document proposes a bounded owner migration for existing administrative
single/bulk customer deletion and JSON subscriber deletion. It does not authorize
deleting accounts, changing retention policy, running migrations, or deploying.

## Accepted pre-cutover behavior and evidence

`tests/integration/test_customer_hard_delete_characterization_pg.py` uses the
existing `cloned_database("heads")` fixture: the real Alembic template is cloned
into a private writable PostgreSQL database. Actual committing entrypoints run
against that clone. Local PostgreSQL validation is collection only; the eighteen
expectations below were accepted by the hosted migrated PostgreSQL run above.
The plural target includes Sub and kernel branch heads; singular `head` is
ambiguous in this migration graph.
Each Subscriber fixture owns an inert test Reseller: migration 116 requires
`subscribers.reseller_id` even though the ORM permits null. Fixture review also
checked baseline required columns and subsequent required-column additions for
the inserted auth, network, subscription, invoice and communication records;
future implementation changes require fresh hosted migrated execution.

| Entry point | Current behavior |
| --- | --- |
| `app/api/subscribers.py:delete_subscriber` → `Subscribers.delete` | No inactive precondition; auth children are not implicitly removed. Their FKs can block deletion. |
| `web_customer_actions.delete_person_customer` | Rejects active accounts; bulk-deletes UserCredential, MFAMethod, Session and ApiKey, **commits**, then calls parent deletion. A later invoice/subscription blocker leaves the account alive with auth children already gone. This is a known defect to fix in the next owner PR. |
| `delete_business_customer` | Rejects any subscription before calling person deletion; then applies the same inactive check and split transaction. |
| `bulk_delete_customers` | Processes input order; retains earlier per-item commits; skips missing, active, and subscribed accounts. Person/subscriber items stage auth removal in the parent transaction; business items use split-commit person deletion. |
| `Subscribers.delete` | Calls `_release_subscriber_network_records`, deletes Subscriber, commits. A later parent FK failure rolls back network changes after caller rollback. |

MFARecoveryCode → MFAMethod is a non-cascading FK. Recovery rows block web
cleanup before its first commit; rollback restores earlier staged credential
deletion. Invoice and Subscription are examples of parent blockers. For the
person web path, Subscriber's ORM subscription relationship attempts to null
`subscriptions.subscriber_id`; its NOT NULL constraint refuses the flush
(SQLSTATE 23502) before parent DELETE. Invoice and recovery tests pin SQLSTATE
23503 and catalog-derived FK names; JSON uses a lone credential to prove its
exact auth-child FK blocker. Assertions inspect only safe diagnostic identifiers,
not SQL, parameters or row details. Preserve
financial/history blockers rather than expanding deletion to make it succeed.

Successful account deletion preserves Party and deployment-global
AuthenticationBinding. CommunicationIntentRecord is subscriber-cascading;
Notification survives with subscriber and deleted-intent references set null.
The preservation test uses an unbound legacy credential: it does not claim
acceptance of a fully projected tenant/Party/binding credential fixture.

### CPE schema discrepancy

`alembic/versions/026_make_cpe_subscriber_id_nullable.py`, revision
`026_cpe_subscriber_nullable`, changes `cpe_devices.subscriber_id` to nullable
and recreates its FK with `ON DELETE SET NULL`. The source migration chain
through 672 contains no later reversal of that column/FK. Later CPE migrations
209/265 add other relationships. The ORM declares `nullable=False`, and
`alembic/versions/squashed_schema.sql` contains the older NOT NULL/FK shape;
neither overrides Alembic's migrated schema authority. Actual deployment shape
has not been verified by this work.

The characterization pins catalog nullable/SET NULL, a direct SQL parent delete
that retains active CPE detached, and the actual `Subscribers.delete` path that
first changes active CPE to retired and then retains it detached. Nonactive CPE
statuses remain unchanged. Active IPv4 and IPv6 assignments become inactive and
detached. Parent financial failure restores the active CPE and assignments.
These are explicit expectations: schema disagreement fails acceptance; there is
no conditional outcome or metadata/SQLite fallback.

`tests/test_ondelete_release.py` only calls the pre-delete helper and never
deletes a parent. Existing TR-069 integration setup removes CPE before parent
cleanup. Neither proves this migrated parent-delete behavior. This PR deliberately
does not repair the model, squashed snapshot, migration, or production schema.

## Coherent owner cutover

Introduce a fully typed `customer.account_deletion` application coordinator and
complete ServiceContract. Typed commands identify the exact account, trusted
actor, request command context and adapter policy; typed outcomes distinguish
deleted, missing, ineligible, blocked and busy. Do not accept a caller-provided
actor bag or infer actor identity from the target account.

Adapters create and close a fresh transaction-free Session. The coordinator
enters `execute_owner_command` exactly once per customer. Single web deletion
stages auth cleanup, network consequences, parent deletion and canonical audit
in the same root transaction. No helper commits, independent ApiKey command,
participant savepoint or post-commit audit may split this boundary.

Bulk remains one transaction per item, retaining earlier successful commits.
Each item gets a fresh Session; a failed item rolls back before the next item.
Do not change bulk to all-or-nothing. Preserve item order and existing inactive,
missing and subscription eligibility results. Count a duplicate target only on
its first successful deletion; a later occurrence is missing and skipped.
Unknown item types are skipped with an explicit reason, never counted as
deleted. A database-blocked middle item rolls back before processing the next
item. These truthful results replace misleading legacy counts.

Bulk commands carry immutable expected active/subscription eligibility facts
from the confirmed impact. Recheck those facts under the Subscriber lock before
any destructive staging. An item becoming eligible after confirmation must not
be deleted under an impact statement that said it would be skipped: report a
sanitized stale-impact result and continue with the remaining items. The same
facts construct the confirmation token and the typed expectations; no separate
unverified snapshot is substituted.

Keep the existing distinctions between web and JSON: web may stage its existing
auth-child cleanup; JSON must not gain implicit auth cascading or an inactive
eligibility rule. Recovery-code, subscription, financial and history blockers
remain blockers. The intentional defect fix is atomic rollback of web auth
cleanup when the parent cannot be deleted.

### Lock ordering and freshness

1. Capture the exact subscriber-bound ApiKey ID set, sort UUIDs, and lock those
   rows before acquiring `Subscriber FOR UPDATE`. Refresh locked key state and
   retain only currently matching keys for deletion. Never delete a transferred
   key merely because it appeared in the capture.
2. Lock and refresh the exact Subscriber, then recheck adapter eligibility.
3. At READ COMMITTED, execute a fresh **nonlocking** key-set query after the
   parent lock. Refuse if any current matching key was not captured and locked.
   Roll back and let a new command recapture. Never lock a newly discovered key
   after the parent: ApiKey PATCH takes key first and may require subscriber FK
   locking; reversing that order would introduce a cycle.
4. Network participants lock their exact child rows with `FOR UPDATE NOWAIT`
   after the parent. A contended child refuses the whole command rather than
   waiting while holding Subscriber. Fresh-session retry starts from capture;
   do not continue from a partially staged failure.
5. Stage auth cleanup, network transitions, parent deletion and audit, flush,
   then let the executor commit once. FK failure and 55P03 busy outcomes are
   mapped after complete rollback; sanitize database details at the adapter.

The parent lock prevents new FK attachment from completing before deletion;
the fresh key-set recheck detects attachments committed before that lock was
obtained. Acceptance must prove transfer/create interleavings on real physical
connections, including stale captures. This is a bounded lock argument, not a
claim of fleetwide deadlock freedom. Inventory auth-child concurrent writers
before implementation and refuse unexplained inversions.

The implementation inventory found principal-first credential administration,
password reset, password/MFA login, customer MFA enrollment, credential Party
binding and session revocation. Session refresh and legacy credential toggles
can instead hold a child first: exact auth-child NOWAIT acquisition must refuse
contention rather than wait while holding Subscriber. Referral credential
enrollment takes Referral before Subscriber; its retained referred-subscriber
FK is an additional blocker that must be checked without acquiring Referral
after Subscriber. Hosted physical-connection tests must exercise these cases;
source inspection alone is not concurrency acceptance.

Preserve ORM parent deletion. Subscriber's ordinary addresses, custom fields,
channels, contacts and NIN verification relationships cascade through the ORM;
nullable ONT references detach. A direct SQL parent DELETE would introduce new
blockers where the migrated FK lacks a database cascade. Lock the exact ORM
consequence rows NOWAIT before staging deletion, including contact projection
descendants and inactive network children. Retained financial, recovery and
RBAC references remain blockers; no broader Party cleanup is authorized.

### Exact participants and ownership

| Concern | Owner boundary |
| --- | --- |
| Customer parent deletion and atomic orchestration | New `customer.account_deletion`; exact single-account command and per-item bulk adapter. |
| Subscriber ApiKey removal | Extend `auth.api_key_administration` with a typed flush-only participant admitted only under the exact customer-deletion coordinator command. Preserve web cleanup policy and JSON compatibility. |
| Existing credential/MFA/session cleanup | Explicit typed auth participant under the same exact context; preserve recovery-code blockers. No global credential, Party or binding cleanup. |
| Hard-delete release of active subscriber IPv4/IPv6 assignments | Extend existing `network.ip_assignment_lifecycle` with an exact subscriber-hard-delete concern and typed flush-only participant. Its existing exact-service repair participant does not already authorize this operation. |
| Active → retired subscriber CPE transition | New narrow `network.cpe_subscriber_retirement` typed flush-only participant and complete contract. Only exact subscriber hard deletion, only active rows; no global CPE lifecycle takeover. |

Participants require the exact owning command definition/name, executing owner,
Session, trusted context and actor to agree. Merely having some active command
context is insufficient. Network child locks use NOWAIT as above; stage no
transport, RADIUS, disconnect, provisioning, repair or global cleanup work.
Account deletion does not remove retained CPE/IP history; existing migrated FKs
detach those rows after release/retirement.

`network.monitoring_inventory` currently declares broad monitoring inventory,
metric/alert and admission ownership without an exact customer-CPE retirement
contract. It is not a ready-made participant. `network.device_state` and device
drift resolvers interpret observations; do not mislabel them as CPE writers.
Register the narrow concern and dependencies without claiming broader inventory
ownership has migrated.

Audit is local canonical evidence committed with the account change. It must
exclude secrets, hashes and unnecessary customer identity details. Preserve
Party, AuthenticationBinding and retained history; no domain event or external
effect is invented solely to justify deletion.

## Implementation acceptance and open gaps

This slice updates registry/map, retired-writer and construction baselines,
architecture guards, focused behavior tests and documentation together. Remove
the legacy fallback for the migrated paths and prove adapters cannot own commits
or invoke participants directly. Enforce exact participant command admission,
executor once, flush-only helpers and fresh per-item Sessions.

Hosted migrated PostgreSQL must execute this characterization and the new
atomic-rollback, audit-failure, exact lock-contention, key-transfer/create race,
network NOWAIT and per-item failure-continuation cases. The known-defect
expectations are replaced with atomic behavior assertions in the same slice.
Do not weaken an explicit schema assertion to accommodate the ORM conflict.

The implementation replaces the split-commit assertions with atomic rollback
and preserves the existing explicit constraint diagnostics. Forty-five migrated
PostgreSQL cases are collected: eighteen characterization, sixteen owner and
eleven network participant cases. Local collection is not PostgreSQL acceptance;
the exact published source must pass those cases in hosted CI before merge.
The fixture census adds exactly eighteen test-only construction sites (ten
owner, six network, and two additional characterization sessions), reaching 322.
Production construction allowances do not grow. Four retired web-adapter
rollbacks shrink that baseline from seventeen to thirteen in the customers
route module.
The cohort detector now recognizes tracked ORM instance deletion. Against the
same detector, accepted source has 133 writer sites and the cutover has 132;
two legacy parent-delete sites become one typed owner. Correcting the previously
missed Address deletion records existing debt without authorizing a new writer.

This bounded suite does not prove every FK/history blocker, projected credentials,
every HTTP authentication mode, or all concurrency interleavings. Fast route
tests exercise the real permission dependency and trusted user/API-key actor
forwarding with upstream authentication isolated. Owner PostgreSQL cases cover
failed-middle-item continuation, key capture interleavings, child contention,
referral enrollment ordering, ordinary ORM cascades and confirmed-impact races;
they do not prove fleetwide deadlock freedom or unknown-commit recovery.
Source/model discrepancy is recorded for a separately authorized schema
investigation. No deployment, production account deletion, local PostgreSQL or
Docker run, RADIUS action or retention-policy change occurred.
