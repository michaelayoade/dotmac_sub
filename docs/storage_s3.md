# S3-Compatible Private File Storage

## Architecture Summary
- Storage provider: S3-compatible backend via `app/services/object_storage.py` (AWS S3, MinIO, Cloudflare R2).
- Metadata source of truth: `stored_files` table (`app/models/stored_file.py`).
- Upload policy + validation: `app/services/file_storage.py` (`UnifiedFileUploadService` + per-domain `DOMAIN_CONFIGS`).
- Legal documents now upload to private object storage and stream through the app; no public object URLs are returned.
- Legacy local files are still readable via safe fallback (`uploads/`-scoped path checks).

## Environment Setup
Set the following:

```env
S3_ENDPOINT_URL=http://localhost:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET_NAME=dotmac-private
S3_REGION=us-east-1
```

`docker-compose.yml` now includes a local `minio` service (`:9000` API, `:9001` console).

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
