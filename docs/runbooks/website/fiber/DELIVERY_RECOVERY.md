# Fiber website delivery recovery and installation

Status: implementation candidate; production installation and real receiver
acceptance are not complete. Selfcare owns committed Leads, origin capture,
coverage decisions and conversions. `DotmacFiberOutbox` owns only the website's
transport record. A pending record is not a saved Lead.

## Durable transport contract

The browser creates a cryptographically random UUID v4. Before its first POST,
it stores identical serialized input in IndexedDB, encrypted with AES-GCM and
a nonextractable WebCrypto key. Web Locks serialize tabs' pending-request
creation/deletion; an existing request wins. No customer payload is placed in
localStorage, sessionStorage, URLs or analytics. This protects stored bytes,
but does not protect against scripts executing with the same origin's access.
HTTPS, IndexedDB, WebCrypto and Web Locks are required; unsupported storage
fails closed before delivery. Clearing site data removes browser recovery;
the server's already persisted outbox remains independent.

The server canonicalizes input for a fingerprint, derives the transport ID
from the submission UUID and stable WordPress auth salt, and freezes the
Selfcare JSON, submitted time, attribution, URL and delivery ID before HTTP.
The signing secret is used only in memory when sending. A changed input under
an existing ID is a conflict, including after successful delivery. Replays do
not invoke the payload builder. Source field limits, paired coordinates,
required attribution and the v1 contract remain enforced.

Records use AES-256-GCM with a domain-separated key derived from WordPress's
auth salt. The operator must provision a dedicated directory outside every
web document root, owned by the PHP/CLI account, mode 0700. Files are mode 0600.
Each submission uses `flock` across processes, encrypted temporary writes,
file fsync, atomic rename and directory fsync. This requires PHP 8.1+, OpenSSL,
and a local Linux filesystem with working flock/fsync/atomic rename. NFS and
multiple independent website hosts are unsupported and must fail deployment
acceptance. Back up salts and this directory together, encrypted. Do not
rotate salts or the signing secret with pending deliveries without an approved
reconciliation plan. Secrets never enter the outbox.

An attempt and its next eligible time are persisted before HTTP. The claim is
held during HTTP; process death releases the lock and the durable reservation
survives. Timeout, 408/425/429, 5xx, malformed receipts and unexpected transport
exceptions remain unconfirmed. Other HTTP rejections or changed target
configuration require operator attention. Retries are at 60 seconds, doubling
to a six-hour cap, at most twelve attempts within seven days. One explicit
operator recovery can authorize another twelve attempts within the original
deadline, retaining the same bytes/ID. It cannot edit, retarget or regenerate
a request. Redirects are disabled. Responses are size-limited and only a
validated reference plus recognized coverage result confirms receipt.

An OS cron worker is required; browser traffic and WP-Cron are not the recovery
guarantee. Each worker run processes at most fifty records. A persistent
lexicographic cursor prevents confirmed records from starving later deliveries;
the worker cursor lock prevents concurrent scans, and per-record locks protect
browser/operator/worker overlap. Inventory exposes only opaque transport ID,
state, timestamps, attempt count and a safe reason code. Corrupt or unreadable
records fail closed and require operator review; never replace them with a new
request. Disk/inode capacity must be monitored. The enqueue guard refuses a
directory with 20,000 entries; allocate a filesystem quota as an additional
host boundary, and alert well before exhaustion.

Unconfirmed payloads expire after seven days and are removed by the worker.
Confirmed payloads are removed after thirty days. Encrypted fingerprint,
delivery identity and safe receipt/state tombstones remain to reject reuse
and return stable receipts. Do not delete tombstones during routine cleanup.
Browser payload cleanup occurs on the next visit after seven days, retaining
only an expired submission ID. A closed browser cannot run a timed cleanup.
No new automatic delivery is made from an expired browser record.

Browser UI treats 202 as pending confirmation. A received UI requires the
same safe receipt validation as the server. Coverage unavailable/out-of-area,
manual review and technical calculation failure have distinct copy. No browser
Lead conversion is emitted. Selfcare alone publishes committed Lead evidence.

## Read-only evidence, 2026-10-11

- Installed MU plugin matches PR #3487's bundle; installed coverage HTML differs
  only by a trailing newline after normalization. No newer website edit was
  discarded. The maintained staging `fiber-site` plugin is older (v1.0.0/v2 wire
  contract) and must not be rebuilt over the corrected installed source.
- WordPress runtime reports all three connection constants undefined.
- Existing binding `8697e0d5-b2b3-4ec4-8c06-2f1e644402b1` and installation are
  disabled, with production environment, correct capability/site and expected
  secret reference. Application runtime resolution of the signing secret fails
  with `HTTPException`. This establishes it is not readable by Selfcare's
  current identity (application exception HTTP 404); it does not establish
  whether the stored secret is absent or incorrectly mapped in OpenBao.
- No production inquiry or test Lead was submitted; no binding or secret was
  changed. The candidate is not installed.

## Installation and rollback proposal — requires review and authorization

1. Review the implementation PR and CI, select an exact source SHA, and record
   the named non-production WordPress URL, Selfcare binding and test-data scope.
   Use the current maintained runbook's expectation: missing coordinates mean
   `manual_review` with a saved Lead. The older PR #3487 snapshot's null-coverage
   acceptance step has already been corrected on main.
2. On the explicitly authorized website host, record hashes of the currently
   installed plugin, page and maintained `fiber-site` sources. Back up all of
   them outside public_html. Recompare against this PR immediately before any
   replacement and reconcile newer edits. Back up configuration without printing
   secret values; do not copy it into the source repository.
3. Verify PHP/openssl/fsync/flock support, one local persistent filesystem,
   account ownership, available disk/inodes and secure backup/restore. Provision
   the private outbox path only after that target/action is authorized. Configure
   `DOTMAC_FIBER_OUTBOX_DIR` through the approved server configuration. No automatic
   directory installation happens on plugin load.
4. An authorized OpenBao operator verifies/provisions the referenced signing
   field and application read access. Use the approved secure exchange to set
   the matching WordPress signing constant, plus URL and existing binding ID.
   Do not print the value, pass it in command arguments, or create another binding.
   Enable the existing binding only under separate explicit authorization.
5. Install the outbox helper and MU plugin together, then the reconciled page.
   Supply the helper in the same MU-plugin directory; it defines no independent
   hooks. Verify syntax before atomic file replacement. Install the reviewed OS
   cron to invoke `wp --url=<approved-site> fiber-outbox run` every minute under
   the same website account, with private sanitized output. Loading the candidate
   alone does not register an automatic schedule.
6. Run `fiber-outbox inventory` to verify state/attempt visibility. Alert for
   `attention`, `expired`, `storage_error`, old pending records, stale worker
   cursor (while records exist), disk pressure and nonzero worker exit. Assign
   a named operator. Check inventory at shift start/end and after outages.
   After correcting a transport/configuration issue, the authorized operator
   may use `fiber-outbox recover <opaque-inventory-id>`; record its reason and
   result in the operator audit. Never copy customer payloads into logs or tickets.
7. Execute the acceptance matrix below against the approved test receiver.
   Confirm exactly one canonical Lead and stable reference using authorized
   Selfcare read APIs/evidence; a website receipt alone does not prove record count.
8. Synchronize `fiber-site/wp-mu-plugins` and `pages/fiber-coverage-page.html` with
   the exact accepted versions, including the helper. Update the builder/publishing
   pipeline to copy the helper and preserve the accepted form owner; compare the
   rebuilt `dist/coverage/index.html` before publication. Production installation
   requires explicit authorization of the target and reviewed files/config/cron.

Rollback: pause intake and the OS worker first. Preserve the entire encrypted
outbox, salts, browser status route and receipt/conflict handling. Inventory
unconfirmed deliveries and reconcile them with Selfcare before any retry from
older code. Do not blindly roll back to v1.1.0: that version regenerates payloads
and has no pending recovery. A visual page rollback is permissible only with the
accepted pending/receipt owner retained. For a code defect, prefer a reviewed
forward fix; restoring backed-up legacy files requires a drained/reconciled
outbox and a separately authorized procedure. Do not reset secrets or remove
tombstones as part of rollback. Resume workers/intake only after safe replay
acceptance and an operator records the outcome.

## Acceptance matrix

Isolated `php test-producer.php`, `php test-outbox.php`, and
`node test-browser.cjs` use fake credentials/receipts and no customer records.
They check v1 payload/signature, optional data, coordinates, required attribution,
receipt validation, frozen replay, encrypted persistence, process restart,
multi-process claiming, bounded attempts, expiry, operator recovery and browser
pending/received states. Browser storage is mocked; its test is not actual
IndexedDB/browser persistence evidence. Mock receiver counters are not Lead counts.

Real WordPress + Selfcare staging acceptance remains mandatory:

| Journey | Required receiver and browser evidence |
| --- | --- |
| Normal submission | One committed Lead, valid reference and appropriate coverage |
| Address without coordinates | One Lead, `manual_review`, no out-of-area claim |
| Coverage calculation failure | One retained Lead, `technical_error`, received reference |
| Response lost after commit | Pending first, same frozen retry, one Lead and same reference |
| Identical replay | Same bytes, delivery ID, reference and one Lead |
| Concurrent replay | Atomic sender claim; receiver deduplication; one Lead |
| Browser closure + PHP/worker restart | Encrypted recovery, no regenerated attribution/time |
| Malformed receipt | Pending; never received or converted |
| Changed content with same UUID | Conflict; original stored request unchanged |
| Invalid signature / disabled binding | No false success, visible operator failure |
| Expiry / storage-full / corrupted record | Visible unconfirmed state; no silent new submission |
| Worker paging / crash during attempt | No starvation; reserved attempt bounded; stable replay |

Repository-wide Python checks and full migrated PostgreSQL/PostGIS integration
tests were not run on the production or staging deployment hosts. Required CI
and approved staging journey evidence remain gates, not completed claims.
