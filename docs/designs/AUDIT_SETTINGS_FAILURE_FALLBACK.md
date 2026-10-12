# Supplementary request audit settings fallback

Owner: request audit middleware in `app/main.py` (A-PR1).

When settings cannot be loaded and no cached settings exist, supplementary
request auditing uses the existing enabled defaults. Each such failure increments
`audit_settings_fallbacks_total`, without identifier or request labels. The
settings session is closed before the downstream request runs.

Fresh and stale settings retain their existing behavior, including an explicit
disabled setting. Health, metrics, static and existing webhook exclusions are
unchanged. Request audit writes remain best effort in their separate session.
This middleware does not provide the transactional record required for protected
mutations; those owner/outbox and failure-event changes are separate slices.

The regression in `tests/test_main_domain_routing.py` simulates an unavailable
settings database, checks that the successful response survives, confirms an
audit attempt, and checks the counter increment. The same suite covers stale
settings reuse and webhook exclusions.
