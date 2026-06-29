# Billing Audit Remediation

Source audit: `docs/designs/BILLING_UX_POLISH_AUDIT.md`
Branch: `audit/billing-remediation`
Dependency order: 6
Status: Draft scaffold only

## PR Readiness Checklist

- [ ] Implement scoped remediation for this audit.
- [ ] Update the source audit document with `Resolved`, `Partially resolved`, `Still open`, and `Deferred` sections.
- [ ] Record automated tests and manual verification against the audit findings.
- [ ] Rebase or merge latest `main` before marking ready for review.
- [ ] Keep this PR as draft until the checklist above is complete.

## Dependency Notes

This PR should follow System / configuration for settings validation work. Customer Portal billing flows, VAS provider/refund work, Reseller billing, CRM billing push, and Reports finance metrics should follow or rebase on this where they depend on billing behavior.
