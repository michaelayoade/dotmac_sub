# Restricted field identity review

`operations.field_identity` owns submission and manual staff review of exact
portrait/evidence revisions linked to existing `SystemUser`. Authentication,
customer avatars, vendor membership and assignment remain separate authorities.
No external KYC provider or live permission grant is introduced.

Subject or authenticated staff holding `operations:technician:write` may submit
and read restricted evidence. Only an independent active reviewer with that
permission may review. Pending becomes reviewed or rejected; reviewed becomes
revoked. Rejected/revoked submissions require a new revision. The reviewer
approves the display name, company, identity evidence and exact portrait together.
There is no independently approved portrait while verification is pending.

Review admission uses the pure `auth.permission_key_matching` vocabulary shared
with route guards, including explicit aliases, domain wildcards and the
administrator `*` grant. A wildcard never removes the independent-review rule.
The restricted detail and file responses use `Cache-Control: private, no-store`.

A SystemUser row lock serializes first and later submissions and review. Expected
revision/version fail closed. Unique subject/idempotency receipts fingerprint
intent, and replay rechecks actor admission before returning original outcomes.
Exactly one owner executor stages file metadata, identity, receipt, audit and
`field_identity.changed` v1 event atomically. Events contain no KYC or file keys.
Retry only whole commands after transient database failure with the same key.
Object preparation happens before locks; failed submissions can leave private,
unreferenced objects requiring the existing object-store retention process.

Objects use existing private file storage with reserved field_identity_portrait
and field_identity_evidence entity types and no subscriber owner. Generic file
access explicitly refuses these entity types; only identity policy allows reads.
No public avatar URL or presigned KYC URL is issued. Reads use no-store responses.

Portrait preparation validates JPEG/PNG/WebP, applies EXIF orientation and
re-encodes a fresh lossless PNG raster without source metadata. Manual review
therefore covers the sanitized stored artifact. Evidence remains separately
restricted and retains its original bytes. Portraits are static, at most 20
million decoded pixels, at most 8192 pixels per dimension, and at most 5 MiB
both before and after encoding; unsupported, malformed or oversized files fail
closed before upload.

Customer delivery uses `approved_portrait_stream`, which repeats current revision
approval and the same normalization before returning bytes. This also protects
previously reviewed originals without mutating their artifacts or manufacturing
a new approval: displayed oriented pixels are preserved losslessly while EXIF,
GPS, XMP, comments, profiles and other source payloads are discarded. Legacy
files that cannot satisfy decoding/size limits are refused and need a new
submission and review. No historical object inspection or bulk rewrite occurs.

Customer assignment query adapters must resolve the current assigned SystemUser
on every card and portrait request, then use approved_customer_card or
approved_portrait_stream with the exact revision. These are internal inputs, not public
customer download endpoints. Only current reviewed revisions of active subjects
produce cards; pending replacement/rejection/revocation suppress cards immediately.
The typed card contains display name, company, reviewed status/time/revision and
an opaque portrait identifier; it never contains review reason or evidence.
Repair is a new reviewed revision, not editing raw files or inferring approval.

Migration672 expands two new tables; no backfill grants approval. Fresh and
671-to-672 PostgreSQL rehearsal is required on the dedicated test server before
schema acceptance. Fast SQLite unit results are non-authoritative.
