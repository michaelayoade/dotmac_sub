# CRM / Identity Audit Remediation

Source audit: `docs/designs/CRM_IDENTITY_UX_POLISH_AUDIT.md`
Branch: `audit/crm-identity-remediation`
Dependency order: 10
Status: Draft scaffold only

## PR Readiness Checklist

- [ ] Implement scoped remediation for this audit.
- [ ] Update the source audit document with `Resolved`, `Partially resolved`, `Still open`, and `Deferred` sections.
- [ ] Record automated tests and manual verification against the audit findings.
- [ ] Rebase or merge latest `main` before marking ready for review.
- [ ] Keep this PR as draft until the checklist above is complete.

## Dependency Notes

This PR should follow Integrations / webhooks, Support, Billing, and Notifications where webhook contracts, ticket behavior, billing currency, dead letters, or notification delivery overlap.
