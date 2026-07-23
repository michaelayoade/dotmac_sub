# Invoice Draft Authoring Boundary

## Decision

`financial.invoice_draft_authoring` owns administrative creation and editing of
the complete invoice draft aggregate. The aggregate includes the invoice header,
all active lines, owner-derived totals, create idempotency evidence, audit
evidence, and the transactional `invoice_created` outbox event.

The admin web route is a parsing and error-mapping adapter. It must release any
read transaction before invoking `create_invoice_draft` or
`update_invoice_draft`. It does not create an invoice header, mutate invoice
lines, recalculate totals, commit, or publish notifications itself.

## Invariants

- Administrative authoring always produces or edits `draft`; issue, announce,
  void, write-off, settlement, and reconciliation use separate named commands.
- A draft contains at least one valid line.
- The account is locked before the invoice and its lines.
- Header, lines, totals, audit evidence, idempotency evidence, and event staging
  commit once or roll back together.
- Issued and terminal invoice documents cannot be edited.
- Proformas remain drafts, cannot consume account credit, are excluded from
  collectible AR/dunning, cannot be paid, and cannot be announced.
- `invoice_created` for a draft is internal evidence and does not request a
  customer notification. An explicit final issue/send event carries
  `invoice_number`, `amount`, and `due_date`.

## Migration

The retired admin path committed the invoice header and then called independent
invoice-line writers, each of which committed separately. The replacement
admits one typed complete-state command through `execute_owner_command`.
The shared billing adapter's CRM/subscription invoice-with-lines path now also
delegates to the invoice owner's single-commit constructor; account credit and
the created event are applied only after its complete line set and totals exist.

Historical prepaid drafts and other pre-existing ambiguous billing records are
not modified by this prevention change. They require a separately reviewed,
dry-run-first reconciliation command with deterministic repair evidence.

## Verification

Focused behavior tests cover successful atomic creation, rollback after staged
lines, idempotent replay, draft-only updates, non-payable drafts/proformas, and
complete notification event context. Architecture tests pin the owner contract
and prevent the admin adapter from returning to direct invoice/line writers.
