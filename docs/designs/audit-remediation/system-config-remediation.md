# System / Configuration Audit Remediation

Source audit: `docs/designs/SYSTEM_CONFIG_UX_POLISH_AUDIT.md`
Branch: `audit/system-config-remediation`
Dependency order: 1
Status: MERGED — Merged via the #565 integration stack (branch audit/system-config-remediation, PR #518). Source audit doc carries the Remediation status section (18 resolved, 2 still open).

## PR Readiness Checklist

- [x] Implement scoped remediation for this audit.
- [x] Update the source audit document with `Resolved`, `Partially resolved`, `Still open`, and `Deferred` sections.
- [x] Record automated tests and manual verification against the audit findings.
- [x] Rebase or merge latest `main` before marking ready for review.
- [x] Keep this PR as draft until the checklist above is complete.

## Dependency Notes

This is a foundational audit. Other audit PRs that add, validate, or consume settings should follow or rebase on this branch once it lands.
