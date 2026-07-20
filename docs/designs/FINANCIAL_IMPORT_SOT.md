# Financial import source of truth

Financial and subscription imports use `ImportRun` as the durable staging and
audit boundary. The legacy wizard may parse and preview these modules, but it
cannot apply them directly.

## Execution contract

1. Upload input into a dry-run `ImportRun`.
2. Parse and validate every row without domain writes.
3. An operator explicitly applies the validated run.
4. The apply run references its source through `source_run_id`; a unique
   constraint permits exactly one apply per validated run.
5. Each row executes through its owning service inside a savepoint and records
   the local entity ID or error in `ImportRunRow`.

Invoice imports call `Invoices`, payment imports call `Payments`, and
subscription imports call `Subscriptions`. The wizard does not construct these
models or ledger entries.

## Financial invariants

- Imported invoices start as draft, issued, or overdue. Paid and partially paid
  states are derived from payment allocations. Void and write-off use their
  explicit domain commands.
- Imported payments require a source external ID and must be succeeded cash.
  Pending, failed, and canceled attempts belong in provider-event history.
- A payment is allocated to the named invoice when supplied, otherwise the
  canonical payment service allocates it or records account credit.
- Historical imports suppress customer-facing notification bursts, while audit,
  event, allocation, ledger, and operational consequences remain active.
- Posted financial imports are never rolled back by deleting rows. Corrections
  use invoice void/write-off, payment reversal/refund, or subscription lifecycle
  commands so compensating records remain auditable.
