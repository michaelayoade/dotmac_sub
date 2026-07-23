# Projects source-of-truth completion

`operations.project_lifecycle` exclusively owns native `Project` and
`ProjectTask` identity, lifecycle, transitions, assignment, scheduling,
relationships, audit evidence, transactional events, and derived-state repair.
Public commands run once through `execute_owner_command` on a transaction-free
adapter session. They lock the project before tasks and use flush-only nested
helpers. Adapters map typed domain errors and never commit business state.

`operations.project_assignment_policy` owns rule matching and candidate
selection. Its output is advisory typed decision evidence. Only the project
lifecycle owner may apply manager, assistant-manager, service-team, primary task
assignee, or task-assignee collection changes. The shared ticket assignment
engine cannot write those fields.

`operations.work_order_commands` remains the writer of WorkOrder bindings. The
project owner validates the native Project-to-ProjectTask side; neither owner
may infer a relationship from a CRM identifier. CRM and other external
identifiers are provenance attributes only and are never decision or join keys.
Vendor installation-project lifecycle and workspace owners retain their
existing, non-overlapping authority.

`ui.project_list_projection` owns the complete typed list query: visibility
scope, search fields, filters, status vocabulary, stable sorting, pagination,
freshness, and action eligibility. Routes serialize inputs and templates render
the returned model without redefining these rules.

Project SLA clocks and normalized task-assignee rows are synchronous derived
projections. Drift is a missing, duplicate, or mismatched derived row. The
project lifecycle reconciler locks the native aggregate, reports drift, and
idempotently rebuilds the projection. Unknown or stale projection state fails
closed for mutation eligibility.

State-changing commands stage audit and versioned domain-event evidence in the
same transaction as authoritative state. Events are delivered after commit by
the durable dispatcher. Retryable database concurrency failures retry the whole
command; validation, authorization, stale evidence, relationship ambiguity, and
idempotency conflicts do not.
