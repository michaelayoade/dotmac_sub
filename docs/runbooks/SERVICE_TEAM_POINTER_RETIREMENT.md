# Legacy service-team pointer retirement

Status: implementation ready; production execution and migration rehearsal
require separate authorization.

Owner: `operations.service_team_pointer_retirement`

## Purpose

Migration `426_service_team_lifecycle` predates the composable service-team
model and still validates the legacy scalar `manager_person_id` column. Some
production rows contain CRM Person UUIDs that are neither native Parties nor
reviewed SystemUser-to-Party bindings.

This runbook retires only those unresolved manager pointers so migration 426
can run. It does not read CRM, create or bind identities, import memberships,
copy teams, grant access, or infer a replacement manager.

## Safety contract

- Audit and planning are read-only and report aggregate counts.
- The private plan contains only the complete exact set of unresolved
  `(team_id, stored_person_id)` pointers and a snapshot digest.
- Execution requires a separate approval bound to the exact plan file, expires
  within 24 hours, and caps the pointer count at 25.
- The owner locks and clears only pointers that still match the approved
  snapshot. Any drift rolls back the whole command.
- Existing membership blockers, workflow-setting blockers, or duplicate team
  names are not repaired by this command and keep the migration gate closed.
- CRM memberships are deliberately not imported. Native Sub membership and
  responsibility commands are the only ongoing writers.

Migration 437 also does not turn a valid remaining `manager_person_id` into
membership or responsibility. The pointer stays as shadow evidence until an
administrator explicitly composes the matching `accountable_manager` through
`operations.service_team_lifecycle`; until then the service-team projection
reports legacy-shadow drift and later column retirement remains blocked.

Keep private plan and approval files outside the repository with mode 0600.
They contain evidence-level identifiers and must not enter Git, logs, prompts,
reports, or durable knowledge.

## Read-only audit

```bash
python -m scripts.migration.audit_service_team_pointer_retirement --check
```

Exit status 2 means blockers remain. `scripts/deploy.sh` invokes this exact
check before Alembic.

## Build the private plan

```bash
python -m scripts.migration.plan_service_team_pointer_retirement \
  --out /approved/local/path/service-team-pointer-plan.json
```

The output path must not already exist.

## Approval

Create a separate mode-0600 JSON file:

```json
{
  "schema_version": 1,
  "plan_digest": "<planner output>",
  "plan_file_sha256": "<sha256 of the exact plan file>",
  "approved_by": "<reviewer identity>",
  "approved_at": "2026-01-01T10:00:00+00:00",
  "expires_at": "2026-01-01T18:00:00+00:00",
  "reason": "<reviewed pointer-retirement reason>",
  "maximum_pointers": 5
}
```

## Apply

```bash
python -m scripts.migration.execute_service_team_pointer_retirement \
  --plan /approved/local/path/service-team-pointer-plan.json \
  --approval /approved/local/path/service-team-pointer-approval.json \
  --actor service:<operator-identity> \
  --execute
```

After apply, rerun the audit. It must report `ready=true` and
`blocker_count=0` before migration rehearsal.

After migration 437, run the lifecycle owner's
`audit_legacy_service_team_shadow` query against the restored staging database.
Its typed issue counts must all be zero before proposing legacy-column
retirement. Manager, region, capability, and member-role classifications are
review evidence only: resolve them through the applicable lifecycle command or
a separately approved retirement step, never by copying authority from a
legacy scalar.

## Rehearsal and rollback

Restore the pre-change production backup to staging, run the audit, execute the
reviewed plan, upgrade through migration 437, and verify service-team,
Workqueue, Inbox, field-job, outage-routing, audit, and outbox behavior.

Before Alembic, a failed command rolls back atomically. After migration 426,
rollback is restore from the verified pre-cutover backup; do not stamp past the
migration or reconstruct retired CRM authority.
