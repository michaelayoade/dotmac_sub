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

Revision `585_consolidated_release_heads` joins the five independent `584`
application migrations without modifying their schema operations. It performs
no database operations itself and adds no locking or retry requirements beyond
its parents. Downgrading the join alone performs no data change; downgrading
parents remains governed by their individual contracts.

Before staging, rehearse both a fresh migrated PostgreSQL/PostGIS database and
the deployed predecessor `583_staff_expense_requesters` to the combined head.
Verify the existing composed-module heads as well as the joined application
head using `make test-integration` with an explicit disposable test database.
Do not interpret SQLite unit tests as database acceptance.

Release through dev validation, the automated rolling version PR, one candidate
build, staging acceptance on `10.120.121.20:8001`, main authorization of the same
digest, and production at `selfcare.dotmac.io`, following the release runbooks.
This document records required gates, not evidence that they have passed.
