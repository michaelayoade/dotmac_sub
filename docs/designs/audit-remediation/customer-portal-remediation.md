# Customer Portal Audit Remediation

Source audit: `docs/designs/CUSTOMER_PORTAL_UX_POLISH_AUDIT.md`
Branch: `audit/customer-portal-remediation`
Dependency order: 11
Status: Draft scaffold only

## PR Readiness Checklist

- [ ] Implement scoped remediation for this audit.
- [ ] Update the source audit document with `Resolved`, `Partially resolved`, `Still open`, and `Deferred` sections.
- [ ] Record automated tests and manual verification against the audit findings.
- [ ] Rebase or merge latest `main` before marking ready for review.
- [ ] Keep this PR as draft until the checklist above is complete.

## Dependency Notes

This PR should follow Auth / sessions for read-only view-as enforcement, Billing for pay states, Notifications for preferences/inbox behavior, Support for ticket handling, and Catalog / services for plan or service-request overlap.
