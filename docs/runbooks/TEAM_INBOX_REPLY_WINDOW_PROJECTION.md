# Team Inbox reply-window projection rollout

## Purpose

Roll out the indexed `InboxConversation.last_qualifying_inbound_at` projection
without changing conversation workflow status or losing the authoritative
message chronology.

## Owner and authority

- `communications.team_inbox_threads` writes the projection in the inbound
  message transaction.
- `communications.team_inbox_reply_window` decides Meta free-form eligibility.
- `communications.team_inbox_maintenance` audits and repairs drift.
- `InboxMessage` chronology remains the repair source; Redis sidebar totals are
  only a bounded, non-authoritative read cache.

## Deployment sequence

1. Confirm migration `710_inbox_reply_window_projection` is the single Alembic
   head for the release.
2. Apply the migration. It adds the nullable column, backfills qualifying Meta
   inbound timestamps in batches of 5,000, verifies exact parity, and builds
   `ix_inbox_conversations_reply_window` concurrently. A parity error blocks the
   migration and must be investigated; do not force the version table forward.
3. Deploy the application containing the transactional writer and indexed
   reader.
4. Run `app.tasks.team_inbox.reconcile_reply_window_projection` with
   `dry_run=true`. Continue with the returned `next_conversation_id` while
   `has_more=true`.
5. Require `drift_found=0` across the complete dry run before declaring the
   cutover healthy. If drift is found, retain the output as evidence, rerun the
   affected batch with `dry_run=false`, then repeat the full dry run.
6. Verify `/admin/inbox` loads, assignment totals become available, and a known
   open and expired Meta conversation show the expected reply-window state.

## Verification queries

On PostgreSQL, confirm the index is ready and valid:

```sql
SELECT indexrelid::regclass, indisready, indisvalid
FROM pg_index
WHERE indexrelid = 'ix_inbox_conversations_reply_window'::regclass;
```

Confirm no Meta projection drift remains:

```sql
WITH expected AS (
  SELECT c.id,
         MAX(COALESCE(m.received_at, m.created_at)) AS projected_at
  FROM inbox_conversations AS c
  LEFT JOIN inbox_messages AS m
    ON m.conversation_id = c.id
   AND m.direction = 'inbound'
   AND COALESCE(m.metadata ->> 'reply_window_qualifying', 'true') <> 'false'
  WHERE c.channel_type IN ('whatsapp', 'facebook_messenger', 'instagram_dm')
  GROUP BY c.id
)
SELECT COUNT(*) AS drift_count
FROM inbox_conversations AS c
JOIN expected USING (id)
WHERE c.last_qualifying_inbound_at IS DISTINCT FROM expected.projected_at;
```

## Failure and rollback

- Migration parity failure: stop the deployment and inspect the mismatched
  chronology. The application has not cut over.
- Post-deploy drift: stop rollout progression, run the bounded reconciler in
  apply mode for the affected batches, and repeat the full dry run.
- Application rollback: roll back the application first. The additive column
  and index are safe to leave in place until a reviewed cleanup; do not drop
  them while any deployed writer or reader references them.
- Never rewrite or delete `InboxMessage` rows to repair this projection.
