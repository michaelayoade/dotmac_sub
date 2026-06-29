# Catalog / Services Audit Remediation

Source audit: `docs/designs/CATALOG_SERVICES_UX_POLISH_AUDIT.md`
Branch: `audit/catalog-services-remediation`
Dependency order: 7
Status: Draft scaffold only

## PR Readiness Checklist

- [ ] Implement scoped remediation for this audit.
- [ ] Update the source audit document with `Resolved`, `Partially resolved`, `Still open`, and `Deferred` sections.
- [ ] Record automated tests and manual verification against the audit findings.
- [ ] Rebase or merge latest `main` before marking ready for review.
- [ ] Keep this PR as draft until the checklist above is complete.

## Dependency Notes

This PR should follow System / configuration for settings work and Billing for default currency and financial behavior. Reseller catalog visibility and Customer Portal plan/service flows should follow or rebase on this where they overlap.
