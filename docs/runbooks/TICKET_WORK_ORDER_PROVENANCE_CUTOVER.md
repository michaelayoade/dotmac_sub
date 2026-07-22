# Ticket-to-Work-Order provenance cutover

## Decision

Historical provenance is preserved. `WorkOrder.crm_ticket_id` remains the
external CRM observation and never becomes a lifecycle authority.
`WorkOrder.origin_ticket_id` is the native, authoritative Ticket relationship.
Migration `401_support_ticket_work_order_provenance` links the two only through
the exact imported Ticket provenance at
`support_tickets.metadata.crm_ticket_id`; it does not erase the CRM value.

## Expand and preflight

Migration 382 already added the nullable native foreign key. Before applying
migration 401, take the normal database backup and run these checks:

```sql
SELECT metadata::jsonb ->> 'crm_ticket_id', count(*)
FROM support_tickets
WHERE NULLIF(metadata::jsonb ->> 'crm_ticket_id', '') IS NOT NULL
GROUP BY 1 HAVING count(*) > 1;

SELECT wo.id, wo.crm_ticket_id, wo.subscriber_id, ticket.id,
       ticket.subscriber_id
FROM work_order wo
JOIN support_tickets ticket
  ON wo.crm_ticket_id = ticket.metadata::jsonb ->> 'crm_ticket_id'
WHERE wo.subscriber_id IS DISTINCT FROM ticket.subscriber_id;
```

Both result sets must be empty. The migration repeats these checks and aborts
without backfilling if either identity is ambiguous.

## Backfill and verify

Apply the migration through the normal Alembic release process. Then verify:

```sql
SELECT count(*) AS exact_candidates_without_native_link
FROM work_order wo
JOIN support_tickets ticket
  ON wo.crm_ticket_id = ticket.metadata::jsonb ->> 'crm_ticket_id'
 AND wo.subscriber_id IS NOT DISTINCT FROM ticket.subscriber_id
WHERE wo.origin_ticket_id IS NULL;

SELECT count(*) AS conflicting_links
FROM work_order wo
JOIN support_tickets ticket
  ON wo.crm_ticket_id = ticket.metadata::jsonb ->> 'crm_ticket_id'
WHERE wo.origin_ticket_id IS DISTINCT FROM ticket.id;
```

Both counts must be zero. Sample linked rows and confirm that `crm_ticket_id`
is still populated. Unmatched CRM work orders remain external provenance only;
they are not guessed into a native relationship.

## Cutover, rollback, and repair

After verification, native reads use `origin_ticket_id`; CRM IDs remain visible
only as provenance. The migration is intentionally irreversible because
removing verified native links would reintroduce ambiguity. If a gate fails,
do not bypass it: reconcile the duplicated Ticket provenance or subscriber
mapping, rerun the preflight, and then rerun the migration. Restore from the
pre-migration backup only if the release itself must be rolled back.
