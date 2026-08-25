# Sub → `dotmac-billing` adoption rehearsal

This package is the isolated S0/S1 rehearsal boundary for Sub's eventual
tenant-plane adoption of `dotmac-billing`. It is not imported by `app.main`,
Sub's Alembic environment, routes, jobs, webhooks, or workers. The legacy Sub
invoice, settlement, allocation, and position writers remain the sole
production authority until a separately authorized coupled-watermark cutover.

The harness consumes the real frozen Billing contracts and pins the exact
candidate pair:

- `dotmac-kernel==0.1.0a69`
- `dotmac-billing==0.1.0a1`

Those artifacts are intentionally unpublished while the release and adopter
PRs are under review, so no lock file is fabricated and no path dependency is
committed. Candidate validation injects the two source trees in a disposable
environment. A clean registry install and lock refresh are release gates after
publication, not permission to weaken the dependency contract.

The package owns only migration evidence: total source disposition,
source-to-contract mapping, isolated shadow invocation, exact reconciliation,
and coupled-cutover readiness. It owns no invoice, settlement, allocation,
balance, tax/FX, GL, provider, collection, subscription, numbering, rendering,
file, or service-access decision.
