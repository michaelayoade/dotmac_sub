# Production error repair — 2026-07-22

Status: implementation and deployment contract

## Scope and owners

- `support.ticket_lifecycle` owns support-ticket number allocation. The locked
  `support_ticket` document sequence is canonical for local numbers; existing
  imported numbers are occupied facts that the allocator advances past.
- `financial.prepaid_funding_reconstruction` remains the only owner of opening
  prepaid funding. Admin, customer, and reseller service-change adapters render
  a typed unavailable state when that evidence is missing; they never
  substitute zero.
- `sales.selfserve` owns native quote reads after cutover. While the declared
  CRM-mirror read phase remains active, its read projection exposes current,
  stale, or unavailable transport state to the web page without changing the
  stable mobile/API payload.
- TR-069 inventory reconciliation consumes one active-ONT serial snapshot per
  pass. Serial matches remain exact and ambiguity remains unlinked.
- The bandwidth poller remains an observer. Persistently unreachable routers
  stay visible in poller-health metrics while connection retries back off to a
  bounded fifteen-minute maximum.
- `network.monitoring_inventory` formats the device-health uptime projection.
  The monitoring template renders that value without page-local arithmetic or
  a second unit interpretation.
- Alembic owns the wireless-mast schema repair. Revision
  `405_restore_wireless_masts` restores the archived table contract
  idempotently and intentionally has a non-destructive, forward-only downgrade.

## Deployment gates

1. Apply revision `405_restore_wireless_masts` through the normal image deploy;
   never create the table manually.
2. Verify the deployed migration head and render a POP detail containing zero
   and multiple mast rows.
3. Create concurrent portal/API/admin tickets with an intentionally lagging
   sequence and verify unique monotonically reserved local numbers.
4. Exercise a quarantined prepaid account through admin, customer, and reseller
   service-change previews and confirmations; every adapter must return an
   explicit unavailable state rather than HTTP 500.
5. Observe at least two TR-069 schedules: the full sync must complete within its
   configured time budget and advance device freshness.
6. Confirm unreachable bandwidth targets remain reported as failing while log
   volume drops according to the bounded backoff.

## Rollback and repair

Application rollback does not reverse revision 405. The table may predate the
revision on fresh databases, so a downgrade cannot prove it owns the data and
must not drop it. Any schema defect is repaired forward. Missing prepaid
evidence is resolved only through the sealed reconstruction workflow.
