# Architecture decision records

Use an ADR when a durable architecture choice affects ownership, security,
cross-domain contracts, migration strategy, operational behavior, or an
approved deviation from `docs/CODING_STANDARD.md`.

## Naming and status

Files use `NNNN-short-title.md`. Copy `0000-template.md`, allocate the next
number, and set one status:

- proposed
- accepted
- superseded by ADR NNNN
- rejected
- retired

An accepted ADR is normative for its stated scope. Update or supersede it when
the decision changes; do not silently contradict it in code or another design
document.

## Required review

The ADR must identify the authority boundary, affected systems, consequences,
verification, migration/cutover plan, rollback or forward-fix policy, and any
review or retirement date. Deviations from the SOT standard require Michael's
explicit approval.
