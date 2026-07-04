# Auth / Sessions Audit Remediation

Source audit: `docs/designs/AUTH_SESSIONS_UX_POLISH_AUDIT.md`
Branch: `audit/auth-sessions-remediation`
Dependency order: 2
Status: MERGED — Merged via the #565 integration stack (branch audit/auth-sessions-remediation, PR #519). Source audit doc carries the Remediation status section (9 resolved, 1 still open).

## PR Readiness Checklist

- [x] Implement scoped remediation for this audit.
- [x] Update the source audit document with `Resolved`, `Partially resolved`, `Still open`, and `Deferred` sections.
- [x] Record automated tests and manual verification against the audit findings.
- [x] Rebase or merge latest `main` before marking ready for review.
- [x] Keep this PR as draft until the checklist above is complete.

## Dependency Notes

This PR should follow the System / configuration foundation where settings registration or validation is involved. Customer Portal and Reseller view-as/session work should depend on this where possible.
