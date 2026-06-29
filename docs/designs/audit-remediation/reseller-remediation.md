# Reseller Audit Remediation

Source audit: `docs/designs/RESELLER_UX_POLISH_AUDIT.md`
Branch: `audit/reseller-remediation`
Dependency order: 12
Status: Draft scaffold only

## PR Readiness Checklist

- [ ] Implement scoped remediation for this audit.
- [ ] Update the source audit document with `Resolved`, `Partially resolved`, `Still open`, and `Deferred` sections.
- [ ] Record automated tests and manual verification against the audit findings.
- [ ] Rebase or merge latest `main` before marking ready for review.
- [ ] Keep this PR as draft until the checklist above is complete.

## Dependency Notes

This PR should follow Auth / sessions for impersonation/session behavior, Billing for partner money display and allocation behavior, Catalog / services for reseller offer visibility, VAS / wallet for shared VAS behavior, and Support/CRM for ticket surfaces.
