# API-key scopes

**Date:** 2026-06-29
**Context:** security-review finding #13. `ApiKey` had **no scopes** — a key
authenticated identity but carried zero permissions, so it could never pass a
permission gate. The audit endpoint had historically accepted *any* active key
without a scope check (an unscoped bypass; closed in #533 by rejecting keys
outright). This adds a real scopes model so keys can be granted specific access and
the gates enforce it.

## Model

`ApiKey.scopes` — a JSON array of permission keys the key may exercise
(`app/models/auth.py`), e.g. `["audit:read"]`. **Empty = fail-closed:** the key
authenticates identity but has no access. Migration `187_api_key_scopes` adds the
column (`server_default '[]'`). Threaded through the schemas
(`ApiKeyBase`/`Create`/`GenerateRequest`/`Update`) so `ApiKeys.generate` /
`ApiKeys.create` persist it.

## Enforcement

`require_audit_auth` (`app/services/auth_dependencies.py`) now re-accepts an
`x_api_key`, but **only when its scopes satisfy the audit gate** — the same
`_has_audit_scope` check JWTs must pass (`audit:read` / `audit:*`). A key with no or
unrelated scopes is rejected (401), so the historical unscoped bypass stays closed
while legitimate scoped (e.g. SIEM) access is restored. Successful key auth now also
stamps `last_used_at` (fixing the previously-dead "Last used" display).

The existing rejection tests still hold (keys without an audit scope → 401); new
tests cover the scoped-accept path, wildcard scope, and the fail-closed cases.

## Admin UI

The create form takes a **Scopes** field (comma/space-separated permission keys,
parsed by `parse_scopes`; default empty = no access). The key list shows each key's
scopes as chips, or a "no scopes" badge.

## Follow-up (out of scope here)

**DONE.** `require_user_auth` now accepts an `X-Api-Key` header when there is no
bearer/session token (`_api_key_principal`): it returns an auth dict with
`principal_type="api_key"`, `roles=[]`, and `scopes=` the key's scopes. So **any**
`require_permission`-gated endpoint honors a key's scopes (wildcard-aware, e.g. a
`billing:*` scope satisfies `billing:invoice:read`; `*` grants all). No roles → no
admin shortcut; access is exactly the scopes.

Safety: keys cannot reach `/admin` (`require_web_auth` is cookie/session-only and
`require_admin_web_auth` is default-deny to `system_user`); the audit endpoint keeps
its own stricter `require_audit_auth` gate; direct (non-DI) callers leave `x_api_key`
as the `Header` sentinel, so the branch is `isinstance`-guarded. Covered by
`tests/test_api_key_principal.py`.
