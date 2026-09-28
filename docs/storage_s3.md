# S3-Compatible Private File Storage

## Architecture Summary
- Storage provider: the configured durable S3-compatible backend via `app/services/object_storage.py`.
- Metadata source of truth: `stored_files` table (`app/models/stored_file.py`).
- Upload policy + validation: `app/services/file_storage.py` (`UnifiedFileUploadService` + per-domain `DOMAIN_CONFIGS`).
- Legal documents now upload to private object storage and stream through the app; no public object URLs are returned.
- Legacy local files are still readable via safe fallback (`uploads/`-scoped path checks).

## Environment Setup
Configure the deployment's durable object-storage service and provide the
following runtime variables through the approved secret/configuration source:

```env
S3_ENDPOINT_URL=<http://host[:port] or https://host[:port]>
S3_ACCESS_KEY=<approved secret reference>
S3_SECRET_KEY=<approved secret reference>
S3_BUCKET_NAME=<private bucket>
S3_REGION=<provider region>
```

`docker-compose.yml` passes these values to the app; it does not supply a
local object-storage service. Avatar durability therefore depends on the
configured external S3-compatible backend and its backup/retention policy.

The MinIO Python client accepts an endpoint URL with `http` or `https`, a host
and optional port, and at most a trailing `/`. Paths, query strings,
fragments, and embedded credentials are rejected. Before deploying a client
change, check the effective `S3_ENDPOINT_URL` against this grammar without
printing credentials, then verify bucket readiness, upload, authenticated
download, and deletion through the app. The change does not rename or move the
configured bucket. If verification fails, restore the previous application
image and keep the existing object store and its objects in place.

## Security Decisions
- Bucket/object access is private-only; no direct object URL exposure.
- Every authenticated download is streamed through API (`/api/v1/files/{file_id}/download`).
- Tenant/org scoping is enforced by comparing current user org to file `organization_id`.
- Safe key construction: `<prefix>/<tenant>/<entity>/<entity_id>/<generated_filename>`.
- Key/path traversal prevention:
  - key segments validated against strict allow-list regex.
  - legacy local fallback constrained to `uploads/` directory.
- Content validation includes:
  - max size limits (pre-upload),
  - extension + MIME allow-lists,
  - optional magic-byte checks per domain.
- `Content-Disposition` filename is sanitized to prevent header injection.
- Upload/download/delete failures are logged for incident auditing.

## Credential Rotation Runbook
1. Generate new access key + secret in your object storage provider.
2. Update runtime secrets:
   - `S3_ACCESS_KEY`
   - `S3_SECRET_KEY`
3. Restart app/worker services.
4. Verify:
   - upload succeeds,
   - authenticated download succeeds,
   - existing objects remain readable.
5. Revoke the old key in the provider.

## Legacy Migration (Local Disk -> S3)
1. Run migration script:

```bash
poetry run python scripts/migrate_legal_files_to_s3.py
```

2. Script behavior:
   - scans legal documents,
   - skips records already migrated,
   - uploads local file to S3,
   - writes `stored_files` metadata,
   - updates legal document file fields.
3. Validate with a sample download from UI/API.

## Subscriber avatar cutover and legacy recovery

New subscriber avatar uploads use the private S3-compatible backend through
`file_storage.prepare_upload` and `stage_prepared_upload`. The owning
`customer.avatar` command commits the selected `Subscriber.avatar_url` and
`StoredFile` metadata together. A new URL is `/avatars/<stored-file-uuid>`;
its unauthenticated reader serves only the active, S3-backed
`subscriber_avatar` record whose owner and entity ID match the subscriber
whose current `avatar_url` selects that exact file. Replaced objects are
retained; metadata is deactivated within the transaction. Orphan-object
reconciliation is a separate storage operation.

The public `POST /api/v1/auth/me/avatar` ingress has an exact Nginx
`location` in both checked-in selfcare configurations. It buffers the entire
request and caps the **whole multipart body** at 3 MiB, including chunked
requests, before FastAPI can spool the file. The avatar file itself is capped
at 2 MiB by the S3 domain and read at most that limit plus one byte by the
authenticated adapter. Other routes retain their existing proxy limits and
headers. This checked-in configuration must be installed and verified at the
edge before claiming runtime enforcement.

`AVATAR_MAX_SIZE_BYTES` may restrict uploads below 2 MiB, and
`AVATAR_ALLOWED_TYPES` may select a subset of JPEG, PNG, GIF, and WebP.
Startup refuses a larger size, an extra MIME type, an empty policy, or a
nondefault `AVATAR_URL_PREFIX`: the fixed S3 domain and exact ingress limit
cannot honor those overrides safely. Migrate such a deployment by inventorying
its effective policy and existing URLs, deciding a supported replacement
contract, updating the S3 domain plus ingress limit and migration handling in
one reviewed change, then restarting. `AVATAR_UPLOAD_DIR` remains a legacy
source-location hint and never enables local avatar writes.

Existing `/static/avatars/<filename>` URLs remain served by the application's
`/static` mount. Do not remove the mount, the source directory, or backup
copies during this migration. The base Compose configuration does not persist
`/app/static`; off-host backup coverage remains unverified. On 2026-09-27,
a read-only inspection of the explicitly named
`selfcare.dotmac.io` runtime found only an `/app/uploads` mount, backed by
`/root/dotmac_sub/uploads`; both the container's `/app/static/avatars` and
host's `/root/dotmac_sub/static/avatars` had zero regular files excluding
`.gitkeep`. That inspection did not count database avatar URL references or
establish external backup coverage. A subsequent read-only, transaction-level
count on the same named runtime found zero legacy, zero modern, and zero other
`Subscriber.avatar_url` values. There are therefore no currently referenced
production avatars to backfill there. External backup coverage remains
unknown, and a missing old file cannot be recreated from its URL.

Migration is operator controlled and **dry-run first**:

1. On an explicitly authorized host, inventory subscriber IDs and avatar URLs
   matching `/static/avatars/%`, and snapshot the exact database revision.
   Separately inventory the matching files in the running container, mounted
   volume, and backups. Report missing files, unsafe paths,
   MIME/signature mismatches, and files over the avatar domain's 2 MiB limit.
   Do not infer that the repository's `static/` folder holds production data.
2. Produce a read-only manifest with subscriber ID, original URL, source
   basename, SHA-256 digest, byte count, extension-derived image MIME, and migration
   status. The manifest's absolute `source_dir` plus each basename identifies
   the verified source path. MIME is cross-checked against the filename
   extension and known magic-byte prefix; full image decoding is not performed.
   Accept only a single basename under the former legacy
   avatar directory identified by deployment evidence: no traversal, encoded
   separators, symlinks, or
   external URL. Review the manifest and backup evidence before any writes.
3. For each reviewed item, use the same avatar owner command with the verified
   image bytes and MIME. Re-read and lock the subscriber, and proceed only if
   its current avatar URL still equals the manifest's old URL; a concurrent
   user upload wins and the item is skipped. Record the resulting
   `StoredFile` UUID and new URL. A rerun skips rows already on
   `/avatars/`; content-addressed S3 keys make retried uploads safe.
4. Verify each migrated URL through the public reader, including its owner,
   entity type, current selection, image MIME, and byte digest against the
   source manifest. Count unresolved and skipped rows explicitly. Retain
   legacy files and backups until every expected row is verified and rollback
   needs have expired. Never delete the old file before the database commit.

The checked-in tool is `scripts/migration/migrate_subscriber_avatars.py`.
Its default mode is read-only inventory. Supply an **explicit absolute source
directory** containing recovered legacy files; the tool never assumes a source
exists in production. It reads the configured `AVATAR_URL_PREFIX`, MIME and
size policy, validates one safe basename per URL, refuses symlinks and invalid
image signatures, limits each run to at most 1,000 rows, and marks missing or
unsafe files unresolved. The four `AVATAR_*` settings were unset in the
2026-09-27 inspected production process, so its defaults apply there; any
other deployment must review its effective values before running the tool.

```bash
umask 077
poetry run python scripts/migration/migrate_subscriber_avatars.py \
  --source-dir /absolute/recovered/avatars > /private/avatar-inventory.json
sha256sum /private/avatar-inventory.json
```

Review that private manifest and its digest. Apply only on an explicitly
authorized environment using the reviewed digest and named operator:

```bash
poetry run python scripts/migration/migrate_subscriber_avatars.py \
  --source-dir /absolute/recovered/avatars \
  --apply --manifest /private/avatar-inventory.json \
  --manifest-sha256 <reviewed-sha256> \
  --actor <named-operator> --reason <reviewed-change-reference>
```

The manifest must have mode `0600`. Apply rechecks source bytes and digest,
passes the inventoried legacy URL as a compare-and-swap precondition to the
owner command, and reads the selected S3 object back to verify its digest.
Rows already migrated with matching bytes are reported `already_migrated`;
changed URLs are skipped. Missing or unsafe rows prevent apply unless
`--allow-partial` is explicitly supplied, and remain reported with exit 2.
The tool does not delete old files or objects. Keep the manifest and apply
report as controlled migration evidence; count unresolved rows before retiring
legacy reads.

An S3 upload can precede its DB commit and leave an orphan when the command
fails. `scripts/migration/report_avatar_orphans.py` offers bounded,
read-only evidence (at most 1,000 `avatars/` keys per invocation, minimum
24-hour age for investigation candidates). It counts **all** active and
inactive `StoredFile` references to each key and reports pagination
truncation; output contains a SHA-256 key digest instead of subscriber-bearing
raw S3 keys. Objects with naive or missing `LastModified` are unaged and
cannot become investigation candidates. Same-byte retries intentionally share
one content-addressed key,
so a deleted row is never proof that its object may be deleted: another
active row may still serve it, and an upload may still be in flight. The report
authorizes no deletion. A future cleanup owner would need repeated age-gated
evidence, zero live references immediately before a claim-protected delete,
and a rule for in-flight uploads before physical cleanup could be safe.

No production migration or physical cleanup is performed by this source
change. An ad hoc bulk SQL update does not satisfy the owner contract.
