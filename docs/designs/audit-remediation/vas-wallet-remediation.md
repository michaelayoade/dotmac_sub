# VAS / Wallet Audit Remediation

Source audit: `docs/designs/VAS_WALLET_UX_POLISH_AUDIT.md`
Branch: `audit/vas-wallet-remediation`
Dependency order: 8
Status: Draft scaffold only

## PR Readiness Checklist

- [ ] Implement scoped remediation for this audit.
- [ ] Update the source audit document with `Resolved`, `Partially resolved`, `Still open`, and `Deferred` sections.
- [ ] Record automated tests and manual verification against the audit findings.
- [ ] Rebase or merge latest `main` before marking ready for review.
- [ ] Keep this PR as draft until the checklist above is complete.

## Dependency Notes

This PR should follow Billing where provider selection, refunds, default currency, or wallet payment behavior overlap. Reseller VAS portal work should follow or rebase on this where it touches shared VAS behavior.
