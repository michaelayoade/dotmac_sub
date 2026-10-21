# Assignment-scoped field job brief

## Contract and owners

`operations.field_jobs` composes a read-only job brief at
`GET /api/v1/field/jobs/{public_id}/brief`. Frozen typed queries/outcomes live
in `app/services/field/job_brief.py`; the outage read capability is in
`app/services/topology/outage_audience_read.py`. Both authorize the active
native field actor and current job assignment through
`operations.field_work_order_access`, including native vendor execution.
No arbitrary incident/customer identifier can bypass job scope.

Work orders own field instructions, access notes and native project/task/ticket
relationships. The brief shows only the linked active project task, its current
status, instructions and template-plan provenance. It does not expand access to
sibling tasks or convert a recommended template into checked execution evidence.
The existing completion owner supplies photo/signature requirements. Native
as-built evidence requirements remain work-order policy. Reading the brief
neither completes tasks nor authorizes work-order completion.

## Shared-outage audience

`network.outage_work_order_handoff` owns the outage-to-work-order issuance link.
`network.outage_lifecycle` owns immutable scope revisions and entered/retained/
left membership. The read capability reuses `latest_scope_revision` and
`revision_audience_subscription_ids`; it never resolves a second live topology
audience or copies audience state onto a work order.

The response distinguishes the immutable issued revision from the latest
persisted revision. The selected scope defaults to current; clients can request
issued explicitly. Audience rows are sorted by subscription UUID and bounded
at 100 per request (default 50). Continuation requires the exact selected
revision ID and an `after_subscription_id`. A changed revision refuses the
continuation so clients restart with a consistent first page. A work order with
multiple outage links selects a deterministic first link unless the caller
chooses a link belonging to that same authorized job.

Counts are affected **subscriptions**, not deduplicated people and not confirmed
downtime. Exact member count and token must agree with the canonical revision;
issued revision identity/sequence/token/target must agree with the handoff link.
Unknown/unresolved scope returns a null count. Missing/corrupt issued provenance
fails closed. Deleted subscriptions retain their immutable membership and count,
with explicitly unavailable customer details. A later revision never replaces
issued provenance silently.

## Operational information and directions

Customer rows expose only subscription/customer identifiers, display name and
service address. Details are current subscription records, not historical PII
snapshots. Billing, account balances, KYC, passwords, contact lists and ticket
internal investigation text are excluded. Ordinary customer jobs retain their
existing singular customer detail; this endpoint's plural list represents the
linked shared-outage subscription audience.

Directions reuse the work-order location projection, preserving its source.
Latitude/longitude must be finite and in range; an invalid/unknown pair remains
absent while an authoritative address can still be shown. Exact issued outage
FDH/base-station targets may supply their native coordinates. A network device
with no direct coordinates remains a named target without a guessed site or
nearby pin. Native-record coordinates are not labeled as a new GPS verification.
Current outage rerooting does not silently redirect the field visit away from
its issued target.

## Freshness, errors and repair

Each request reads current authorization and existing records without writing
state. Latest means latest persisted revision, not continuously recomputed live
impact. Membership inconsistency, provenance drift and changed pagination are
explicit domain errors. Rebuild is the same authorized read, starting with a
first page; only the existing outage lifecycle/handoff owners repair their
records. No database migration or new authority is introduced.

The brief supports fiber rollout and radio-fiber deployment through existing
project/task relationships. Stored explicit purpose/technology/source/version selects the existing typed
`operations.work_order_templates` recommendation. Legacy unclassified records
remain unclassified; unknown kind or invalid classification has no guessed
recommendation. A saved version mismatch explicitly withholds the current
catalog recommendation instead of silently replacing it. The mobile detail
shows available recommendations in a collapsed Suggested preparation section;
these remain optional planning evidence. End-to-end
repair/deployment checklist enforcement is not claimed by this read slice.

## Verification

`tests/test_field_job_brief.py` covers assignment refusal, inactive actors,
other-job link refusal, multi-customer and deleted-subscription membership,
issued/current revision provenance, stable pages, revision drift, unresolved
impact, invalid job coordinates, exact issued asset directions and HTTP PII
allowlisting. These are fast metadata/SQLite unit tests, not migrated PostgreSQL
acceptance. Architecture tests pin the existing owner, prohibit writes and live
impact recomputation, and guard explicit customer/device column selection.


The mobile detail composes `JobBriefPanel` with the existing job detail. Read
failures show retry and never imply an empty or zero audience. Scope toggles
reset pagination; continuation errors retain the displayed rows and offer a
first-page reload. The shared `field_mobile/test/fixtures/job_brief.json` is
validated against the real server dataclass and consumed by Flutter model,
repository and widget tests. Navigate uses the existing URI launcher and job
location handoff; no new location permission request is added.

## Vendor map consumption

`operations.field_jobs.list_destinations` uses the typed brief directions for
vendors after locking and revalidating the exact current selected member and
non-null assignment generation. It never appends nearby plant. In addition to
the job site and exact issued-outage target, `project_target` resolves only the
job's native Project/task infrastructure. Migration-676 receipt targets must
still agree with that current scope. Device/OLT/PON identity without verified
coordinates stays a label; no adjacent or inferred parent-site pin is returned.
See `VENDOR_JOB_MAP_PRIVACY.md`.
