# Production deployment

`scripts/deploy.sh` is the production deployment owner. It deploys one immutable
GHCR image and keeps the database, proxy handoff, application health, and
rollback boundary in one operation.

## Host contract

- `nginx/selfcare.dotmac.io.conf` is installed and `nginx -t` passes.
- The primary upstream is `127.0.0.1:8001`.
- The deployment-only backup upstream is `127.0.0.1:18001`.
- `.env` contains the production service configuration and approved secret
  references. Secret values are not copied into deployment commands or logs.
- The database backup and deploy locks are writable.

The deployment refuses to start if the running Nginx configuration does not
contain the backup upstream.

## Release sequence

1. Verify the image exists and its OCI revision matches the requested SHA tag.
2. Back up the database.
3. Pin the immutable image and revision.
4. Apply `alembic upgrade heads`, retrying bounded PostgreSQL lock timeouts.
5. Verify registered schema contracts and reject every invalid or unready
   user-schema index.
6. Start and health-check the new application image on `127.0.0.1:18001`.
7. Recreate the primary application and workers. Nginx uses the healthy
   candidate while the primary port is unavailable.
8. Verify the primary image has no source-code bind mount and wait for its
   health endpoint.
9. Gracefully drain the candidate and retain the configured rollback images.

The candidate runs the same image, environment, and database schema as the
primary. It is bound to localhost and exists only for the handoff window.

## Migration/index invariant

Concurrent PostgreSQL index creation is not complete until the catalog reports
both `indisvalid` and `indisready`, and the index definition matches its
checked-in structural contract. A retry must remove an interrupted build before
recreating it; index-name existence alone is not success.

Run the read-only verification independently with:

```bash
docker compose -f docker-compose.yml run --rm --no-deps app \
  python -m scripts.migration.verify_schema_contracts
```

## Failure behavior

- Migration or schema verification failure occurs before service replacement.
- Candidate startup failure leaves the primary release serving traffic.
- Primary health failure restores the previous image while the candidate
  continues serving, then removes the candidate after the rollback is healthy.
- Database migrations are forward-only and are not rolled back automatically,
  so every release migration must remain compatible with the previous image.
