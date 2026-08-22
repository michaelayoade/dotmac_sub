# Team Inbox Reconciliation Runbook

Use this runbook after deploying migration 404, after provider retry incidents,
or when Inbox counts/realtime views disagree. It does not authorize production
access; the target host must be named explicitly before any production work.

## Safety

- Do not replay a provider webhook directly into conversation writers.
- Do not edit conversation, message, contact-link, read-cursor, receipt, or
  counter rows by hand.
- Do not copy private provider payloads into tickets, logs, reports, or PRs.
- Keep Support tickets and Inbox conversations separate; reconcile each through
  its own owner.

## Checks

1. Confirm Alembic has one expected head and migration 404 is applied.
2. Compare observation counts by provider, kind, and processing status. A
   recorded observation without `processed_at` is a processing work item, not
   permission to bypass the processor.
3. Review identity collisions separately in
   `inbox_provider_observation_collisions`. Group by `changed_fields` and inspect
   the retained candidate only through approved, access-controlled tooling.
   Do not copy message bodies or addresses into tickets or logs. A changed
   `payload.body` is a true semantic conflict and remains quarantined; SMTP
   authentication or relay-hop changes alone must replay under semantic v2.
   Repeated delivery of one semantic candidate increments `attempt_count`
   instead of creating unbounded quarantine rows.
4. Re-run the observation processor by observation UUID. Exact completed
   observations return `already_processed`.
5. For delivery drift, compare the Inbox message provider identity with receipt
   observation time and rank. Reapply through the receipt processor; never set
   `delivery_status` manually.
6. Run `rebuild_operator_read_state` for the affected person or all operators.
   The repair removes impossible cursors; it does not guess that a message was
   read.
7. Rebuild realtime with `rebuild_conversation_projection`. Then force the
   client to refetch the database projection.
8. Run the maintenance owner for media promotion, failed-intent retry, or stale
   conversation policy as the specific incident requires.

## Verification

- Repeated processing changes no authoritative row.
- A quarantined SMTP collision receives success only after its candidate row is
  committed and never creates a second Inbox message.
- Older receipts cannot regress the current delivery state.
- Queue KPIs equal their linked filtered cohorts.
- Unread counts equal conversations with inbound messages newer than the
  operator cursor.
- Contact links still satisfy reviewed Party/customer scope.
- Redis failure changes latency/freshness only; database list/detail reads stay
  correct.

Escalate unresolved provider identity collisions, ambiguous contact matches,
parallel Alembic heads, or any temptation to merge Support and Inbox behavior
as architecture decisions rather than applying manual data fixes.
