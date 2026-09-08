# September 8 consolidation validation

The consolidation includes PRs 2976–2988 and 2993–3001 that were open when
the batch was selected. PR 2992 is excluded. The saved managed-connectors
change is included using the newer installation service and existing UI tests.

## Combined behavior

AI-owned conversations require explicit human takeover. Takeover must satisfy
team membership, online presence, FIFO order, and global agent capacity. Failed
assignment rolls back the AI stop. AI handoff provenance passes through both
automatic assignment and queue admission. Queue notices retain lifecycle
suppression and handoff notices; outbound AI notices retain ownership checks.

Classifier failures retain reason and failure-kind evidence, retry limits and
exhaustion, while the newer response policy retains expected facts, affect
assessment, and question planning. Readiness, operational logging, and payment
failure metrics remain separate observations.

## Database acceptance

The application migrations form one linear sequence from
`583_staff_expense_requesters`, as required by the migration sequence gate:

1. `584_customer_backed_quote_delivery`
2. `585_quote_payment_review`
3. `586_inbox_sla_rules`
4. `587_field_request_requester_history`
5. `588_team_inbox_queue_correctness`

The unpublished branch migrations were renumbered and linked before release;
their schema operations and individual locking, retry, and downgrade contracts
are unchanged. The former merge-only revision is removed. An existing developer
database using the superseded branch revisions must be recreated as a disposable
test database; do not stamp it as the new sequence or treat it as release evidence.

Before staging, rehearse both a fresh migrated PostgreSQL/PostGIS database and
the deployed predecessor `583_staff_expense_requesters` to the combined head.
Verify the existing composed-module heads as well as the final application
head using `make test-integration` with an explicit disposable test database.
Do not interpret SQLite unit tests as database acceptance.

Release through feature-branch validation, protected merge to `main`, validation
of the exact `origin/main` commit, the automated rolling version PR, one candidate
build, staging acceptance, and authorization and production deployment of that
same digest. Follow `docs/runbooks/STAGING_PROMOTION.md`; Michael must name the
staging and production hosts before deployment.
This document records required gates, not evidence that they have passed.
