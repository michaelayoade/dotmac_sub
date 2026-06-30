# Billing Audit Remediation

Source audit: `docs/designs/BILLING_UX_POLISH_AUDIT.md`
Branch: `audit/billing-remediation`
Dependency order: 6
Status: Review-ready after 2026-06-30 finish-up; deploy pending branch clean/merge.

## PR Readiness Checklist

- [x] Implement scoped remediation for this audit.
- [x] Update the source audit document with `Resolved`, `Partially resolved`, `Still open`, and `Deferred` sections.
- [x] Record automated tests/manual verification that could run in this environment.
- [x] Rebase or merge latest `main` before marking ready for review.
- [ ] Run full `pytest`/`ruff` in an environment with project dependencies installed.
- [ ] Merge/deploy after branch review.

## 2026-06-30 Finish-Up

- Reapplied the reverted billing audit remediation on top of current `main`.
- Added `/admin/billing/health`, a read-only operator page for billing health
  signals, integrity-launch blockers, runner heartbeats, and autopay failures.
- Added a focused view-model test for the Billing Health autopay summary.
- Verified Python syntax with `compileall` and parsed the new/changed templates.
- Full local `pytest`/`ruff` did not run because the host Python lacks `pytest`
  and Poetry's sandbox-created venv has no installed `pytest`/`ruff`.

## Dependency Notes

This PR should follow System / configuration for settings validation work. Customer Portal billing flows, VAS provider/refund work, Reseller billing, CRM billing push, and Reports finance metrics should follow or rebase on this where they depend on billing behavior.
