# Auth / Sessions Audit Remediation

Source audit: `docs/designs/AUTH_SESSIONS_UX_POLISH_AUDIT.md`
Branch: `audit/auth-sessions-remediation`
Dependency order: 2
Status: Draft scaffold only

## PR Readiness Checklist

- [ ] Implement scoped remediation for this audit.
- [ ] Update the source audit document with `Resolved`, `Partially resolved`, `Still open`, and `Deferred` sections.
- [ ] Record automated tests and manual verification against the audit findings.
- [ ] Rebase or merge latest `main` before marking ready for review.
- [ ] Keep this PR as draft until the checklist above is complete.

## Dependency Notes

This PR should follow the System / configuration foundation where settings registration or validation is involved. Customer Portal and Reseller view-as/session work should depend on this where possible.
