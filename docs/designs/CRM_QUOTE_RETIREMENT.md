# CRM quote retirement

CRM was intentionally cut off. `sales.crm_quote_retirement` owns the immutable
retirement decision. ERP remains a separate active integration. Native Selfcare
quote reads, authoring and payment owners retain their existing feature controls;
this change neither enables nor disables native quoting.

Migration 578 disables every persisted schedule with either retired quote task
name, including differently named aliases. Scheduler reconciliation repeats this
idempotently. Both task names remain broker-compatible tombstones: they return
one typed retirement outcome without opening a session or processing subscribers.
The old service reconciliation entry points also do nothing.

Historical QuoteMirror and QuoteSyncState rows, payloads, IDs and timestamps are
preserved. Mirror reads never fetch CRM, enqueue a refresh, or move synced_at.
Web reads explicitly label the saved information historical; API mirror reads add
source_state=retired and actions_available=false. Old mobile versions may ignore
those additive fields, but request/accept/deposit preflight always refuse through
the existing safe domain-error boundary, regardless of a leftover enabled binding.
No new payment is taken by the retired deposit path. Previously received payments
requiring manual reconciliation must be handled by the financial owner, not by
replaying CRM acceptance or changing historical rows.

Quote-specific CRM client/facade/runner methods are removed. The current CRM
manifest is 1.3.0 without quote_command; all published older pins remain immutable
compatibility facts. No installation is enabled or automatically adopted.

## Remaining dependencies and decisions

The shared CRM connector cannot be removed: subscriber and ticket observations,
portal-session transport, referrals compatibility, and inbound events still have
callers in crm_capability, crm_portal, crm_webhooks, and integration adapters.
Their business replacement/retirement is a separate slice. The quote webhook
admission and historical upsert helper remain because inbound history and native
quote deposit synchronization still reference them. This change sends no
notifications and does not enable an inbound binding. ERP work-order mappings
retain legacy CRM project/ticket identifiers when native references are absent.

Customers needing a new quote while native quoting is disabled are directed to
support; no replacement data source or pricing decision is invented here.

## Validation and rollback

Focused tests cover queued tasks, absent/disabled/enabled bindings, refusal audit,
history and timestamp preservation, scheduler aliases, and retirement copy.
Native quote tests remain required. Before publication, run the repository suite;
run browser/mobile checks before release. A local template render is not browser
acceptance. Full database/migration/concurrency acceptance requires migrated
PostgreSQL, never SQLite metadata.

Do not roll back to a worker that calls the retired CRM. If another change needs
rollback, retain the quote tombstones and schedule retirement. Preserve migration
578 storage and all historical quote and synchronization rows.
