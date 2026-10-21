# Staff project engineering and infrastructure issuance

The `operations.project_engineering_plans` owner supplies protected typed workspace
reads. Overview/history requires `project:engineering_plan:read`; policy, draft,
review and revocation pages require their exact policy/write/approve/revoke
permission. Canonical Project/task business labels accompany typed origin tokens; UUIDs are evidence identifiers rather than user-facing names. Archived or superseded Project/task origins and removed targets remain
readable for history and explicit approval revocation. Draft and review use current
native origin bindings. Project policy explicitly records either reviewer
independence choice; no implicit policy is published by visiting a page.

A separate protected dispatch GET capability discovers only the exact current
approved revision, decision, digest, native target, location and criteria. It
requires fresh `operations:dispatch:write` admission, exposes no draft/history,
and never enters an owner command executor. Its ActionReadiness uses the same
freshness resolver as issuance. Project on_hold or task blocked/done prevents new
physical issuance. The existing handoff command independently rechecks everything.

`ui.project_engineering_projection` composes immutable owner snapshots into
ActionForm/FormContract presentation. It has no ORM or domain policy. Every action
shows consequences, a reason and confirmation before one primary submit. Staff API
and admin adapters use shared frozen extra-forbid request schemas, server-produced
admission and CommandContext actor/scope. Stable intent UUIDs bind command identity
and idempotency. Web POST requires a matching CSRF cookie/form token. Invalid and
stale forms preserve entered values, original intent, and original preview tokens;
stale submit is disabled until explicit reload. Issuance retains exact target and
location evidence for canonical historical receipt replay after fresh authorization.

The staff route family is `/admin/projects/{UUID}/engineering`, with `/policy`,
`/draft`, `/review`, `/revoke`, `/issuance` actions and exact optional task UUID plus
fiber/radio technology query scope. API equivalents use the authenticated projects
family. Dispatch issuance has no engineering READ floor. Minimal Project/task
links expose native infrastructure issuance alongside existing customer work
creation. Subscriber/fiber/Ticket flows are unchanged.

Issuance creates a draft native WorkOrder. Assignment, customer notifications,
provisioning, financial changes and project completion remain separate. API returns
the typed historical receipt. Browser results disclose the completed explicit action;
canonical WorkOrder navigation must use its own dispatch READ permission.

Completion freshness is separate from new issuance eligibility.
`stage_current_for_completion(CompletionEngineeringPlanQuery)` requires active
`operations.field_completion` and fresh `operations:completion-review:write`;
`resolve_current_for_completion` is a narrow current-evidence read requiring
`operations:completion-review:read`. Both preserve exact current approval, digest,
origin/template/inventory binding and revocation checks while allowing current
on_hold/completed Project or blocked/done task status. Historical completion and
revocation reads do not invoke this current-approval participant. Both withdrawal owners reuse the public typed `projects.engineering_plan_revocation_scope` capability, which locks the exact historical Project/task pair without active/status/template/target eligibility.

Fast unit and composed TestClient/template tests are non-authoritative for database
locks/migrations. Root owns PostgreSQL and hosted-browser acceptance. No schema
migration is part of this adapter slice.

## Physical project setup

Create or edit the physical Project first. Infrastructure selection is available
for the six explicit Project-owner types, including fiber and radio installation
and relocation. The picker consumes that owner's eligibility values, not a
second JavaScript policy. Select the native target, then follow the existing
engineering policy, draft, review and issuance pages. Customerless rollout uses
a reviewed native site/asset plus its authored surveyed scope. Generic project
completion remains blocked for enrolled work until explicit accepted-field
completion succeeds; selecting a type or asset supplies no acceptance.
