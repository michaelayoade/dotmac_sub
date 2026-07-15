# Legacy Feature-Alias Retirement

Status: approved for immediate cutover by Michael on 2026-07-15. There is no
post-validation shadow or observation window.

This runbook removes legacy environment and database aliases after the
canonical control implementation passes its release checks. The canonical
Module Manager control becomes the only runtime value source in the same
release.

## Ownership

- Decision owner: `control.feature_registry`.
- Canonical writer: Module Manager tri-state control (`Inherit`, `On`, `Off`)
  through `control.domain_settings`.
- Runtime precedence: active `modules.<canonical_key>` row, then the registry
  default. Retired environment variables and domain-setting rows are ignored.
- Operator surfaces: Module Manager for mutation and provenance; read-only
  Control Plane for cross-domain effective state, health, and audit history.

`billing.billing_enabled` is excluded from deletion. It remains the independent
cross-feature billing master and is no longer treated as the editable value
source for `billing.invoicing`.

## Release gate

All of the following must pass before deploying the immediate cutoff:

1. Every registered feature resolves from its canonical row or declared
   registry default, including scheduler and webhook callers.
2. Existing legacy database values are deterministically materialized as
   canonical rows when no canonical decision already exists. Existing canonical
   decisions win.
3. Every deployment environment alias is inventoried while the old release is
   still running. If an environment value changes the old effective state, the
   equivalent canonical `On` or `Off` value is written before deployment.
4. Legacy settings forms, API fields, seeds, specs, and direct consumers are
   removed or routed to the canonical control.
5. Focused behavior, architecture, migration, scheduler, webhook, and domain
   regression tests pass on the designated test server.
6. The effective-state snapshot and relevant domain health indicators match
   after migration.

Database migration 284 can preserve database rows but cannot discover a value
that exists only in deployment configuration. Therefore item 3 is a mandatory
production pre-deploy gate, not an observation period.

## Immediate cutover sequence

1. On the old release, capture each control's effective value, source, owner
   module, and relevant health indicators.
2. Materialize every effective environment-only decision with the canonical
   Module Manager writer. Do not print or record unrelated configuration or
   secret values.
3. Remove the retired feature environment variables from deployment
   configuration. Keep `BILLING_ENABLED` only where it is intentionally used as
   the independent billing master.
4. Deploy the canonical-only resolver and migration 284 together. The migration
   copies remaining active legacy database values, preserves existing canonical
   rows, then deletes retired rows.
5. Re-run effective-state parity, scheduler/webhook behavior, and domain health
   checks immediately. Once these pass, the legacy path is cut off; there is no
   30-day shadow phase.
6. Record the canonical values, pre/post health result, migration revision,
   approver, and deployment time in the normal audit/change record.

## Failure and rollback

If parity or health fails, stop the rollout and restore the previous release.
Do not recreate legacy rows: migration 284 has already preserved their decisions
as canonical rows, which the previous resolver can read. Correct the canonical
value or implementation, repeat the release gate, and cut over again only after
it passes.

This runbook does not authorize production access or deployment. The production
target must be named explicitly before the pre-deploy inventory or cutover.
