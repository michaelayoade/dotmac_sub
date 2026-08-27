# Field-mobile OIDC federation

Owner: `auth.oidc_mobile_federation` (`app/services/oidc_mobile_federation.py`).
Adapter: `app/api/oidc_mobile.py`. Status: shipped dark — the control defaults
OFF and the identity-provider client stays disabled until the mobile wave that
enables it.

## The boundary, in one sentence

Sub is the **session authority**; the identity provider is a **transport** that
carries exactly one fact — *this issuer authenticated subject S* — and nothing
else it says has any effect here.

Concretely: roles, groups, `realm_access`, `resource_access` and every
authorization scope in the assertion are not read, not stored and not
projected. That is a shape, not a policy — there is no code path from a token
claim to an authorization decision. A technician's permissions come from Sub's
own grants and only from there.

## Two endpoints

### `POST /api/v1/auth/oidc/mobile/start`

Request (`extra="forbid"`):

| field | type | notes |
| --- | --- | --- |
| `code_challenge_method` | `str`, default `"S256"` | Only `S256` is accepted. |

`X-Device-Id` is read from headers if present.

Response:

| field | type |
| --- | --- |
| `ceremony_id` | `UUID` (opaque) |
| `issuer` | `str` |
| `client_id` | `str` |
| `redirect_uri` | `str` |
| `audience` | `str` |
| `scope` | `str` (`"openid"`) |
| `nonce` | `str` (shown once; only its SHA-256 is stored) |
| `code_challenge_method` | `str` (`"S256"`) |
| `expires_at` | `datetime` |
| `expires_in_seconds` | `int` |

It creates no user, no session and no credential.

### `POST /api/v1/auth/oidc/mobile/exchange`

Request (`extra="forbid"`): `ceremony_id: UUID`, `id_token: str`,
`client_id: str`, `redirect_uri: str`.

Response: `access_token`, `refresh_token`, `token_type` (`"bearer"`),
`principal_type`, `principal_id`, `ceremony_id`.

Refusals are `401` with `{"detail": {"code": "...", "reason": "<category>"}}`.
An incomplete deployment configuration is `503`.

## What Sub never receives

* **The PKCE verifier.** The device generates it, keeps it, and exchanges it
  directly with the identity provider. Neither request model has a field for
  one, and both forbid extra fields — so a client that sends `code_verifier` by
  mistake gets a `422` rather than silently handing Sub a secret it must not
  hold. The guarantee is a schema, not a review comment.
* **The authorization code.** The device redeems it at the identity provider
  and sends Sub only the resulting ID token.
* **The nonce, at rest.** Only `nonce_hash` is stored. A database copy of the
  nonce would let anyone with read access mint an assertion that satisfies the
  replay check.

## Configuration is selected from the deployment, never from the caller

`app/services/oidc_mobile_config.py` reads nothing from a request body, a
header or a token claim. An attacker who fully controls both the ceremony
request and the assertion still cannot change which issuer is trusted, which
client id is expected, which redirect URI is bound, or which installed verifier
answers.

Every identifier declares `inherits=False`: a platform row must not stand in
for a missing tenant row, because a less-specific answer to "which issuer"
names the *wrong* identity rather than a weaker one.

| setting (`auth` domain) | env bootstrap | inherits | default |
| --- | --- | --- | --- |
| `oidc_mobile_issuer` | `OIDC_MOBILE_ISSUER` | no | — |
| `oidc_mobile_client_id` | `OIDC_MOBILE_CLIENT_ID` | no | — |
| `oidc_mobile_redirect_uri` | `OIDC_MOBILE_REDIRECT_URI` | no | — |
| `oidc_mobile_audience` | `OIDC_MOBILE_AUDIENCE` | no | — |
| `oidc_mobile_binding_key` | `OIDC_MOBILE_BINDING_KEY` | no | — |
| `oidc_mobile_deployment_id` | `OIDC_MOBILE_DEPLOYMENT_ID` | no | — |
| `oidc_mobile_jwks_source` | `OIDC_MOBILE_JWKS_SOURCE` | yes | `discovery` |
| `oidc_mobile_jwks_uri` | `OIDC_MOBILE_JWKS_URI` | no | — |
| `oidc_mobile_jwks_min_refresh_seconds` | `OIDC_MOBILE_JWKS_MIN_REFRESH_SECONDS` | yes | `300` |
| `oidc_mobile_jwks_timeout_seconds` | `OIDC_MOBILE_JWKS_TIMEOUT_SECONDS` | yes | `5` |
| `oidc_mobile_ceremony_ttl_seconds` | `OIDC_MOBILE_CEREMONY_TTL_SECONDS` | yes | `300` |
| `oidc_mobile_clock_skew_seconds` | `OIDC_MOBILE_CLOCK_SKEW_SECONDS` | yes | `60` |
| `oidc_mobile_max_assertion_age_seconds` | `OIDC_MOBILE_MAX_ASSERTION_AGE_SECONDS` | yes | `300` |

The redirect URI is the permanent, fleet-owned callback. It is deliberately not
derived from Sub's own hostname: the mobile artifact's identity must not depend
on where Sub happens to be deployed. It is compared for **exact equality** at
exchange — no prefix match, no wildcard, no trailing-slash tolerance, no scheme
coercion, no case folding.

### Required, but conditionally

None of these declares `required=True`. The kernel's `required_at` is
unconditional — it fails the **deployment's** boot — and federation is off by
default, so an unconditional requirement would refuse to start every deployment
that will never federate.

The requirement is conditional and enforced where the condition is legible:
`require_federation_config` refuses to build a configuration with any
identifier missing (naming *every* missing key at once), and
`app.main._startup_preflight` calls it whenever the control is on. Missing
configuration is therefore **loud at boot** for a deployment that has turned
the mechanism on, and silent for one that has not.

### There is no client secret

A public mobile client has nowhere to keep one. PKCE is the proof of
possession, and it is enforced by requiring `S256` at ceremony start —
`plain` is not a weaker `S256`, it is the absence of the protection, so it is
refused rather than configurable.

## The feature control

`auth.oidc_mobile_federation` (`app/services/control_registry.py`), layer
`feature`, `default=False`, `on_missing=False`. Consumed by
`oidc_mobile_config.federation_enabled`, which both endpoints call first. Off
means: no ceremony row is written, no request is made to the identity provider,
and both endpoints refuse.

It has no `owner_module`. Federated login is an authentication mechanism, not
an optional product module, and gating it behind a module toggle would make
disabling that module silently disable a login path.

## Admission: the conjunction, with no partial credit

1. Ceremony exists, is unexpired, and is unused (`SELECT … FOR UPDATE`, re-read
   under the lock).
2. Algorithm is asymmetric and exactly allowed — `RS256`. Checked from the
   header **before** a key is looked up, so `alg: none` never reaches key
   resolution and cannot even cost an outbound request. `none` and the HMAC
   family are not weaker configurations, so the allowlist is code, not a
   setting.
3. Signature verifies against the pinned issuer's JWKS.
4. `iss` equals the pinned issuer (checked by the library and again by hand —
   it is the one claim worth checking twice).
5. `aud` contains the configured audience.
6. `azp` equals the configured client **when `aud` is multi-valued**. Without
   this, an assertion minted for a different client that happens to list our
   audience would be admitted.
7. `exp`, `nbf`, and an `iat` that is not implausibly old. `exp` alone is the
   identity provider's opinion of freshness; `oidc_mobile_max_assertion_age_seconds`
   is Sub's.
8. The token's `nonce` matches the ceremony's stored hash, compared with
   `hmac.compare_digest`. A byte-by-byte comparison leaks how much of a guessed
   nonce was right, and the nonce is the anti-replay binding.
9. Every pinned binding — ceremony, client, redirect, deployment — matches for
   exact equality.
10. The verified subject has exactly one **active** credential against the
    installed verifier, and it resolves to an eligible staff principal.

Then, in this order: burn the ceremony and resolve the principal (one owner
command, which commits), then mint the session through
`auth_flow.issue_session_tokens` — the one issuance owner, which commits its
own session row.

They are two transactions because `_issue_tokens` owns its own commit and an
owner command refuses a helper commit inside its boundary. The ordering that
falls out is the safe one: if minting fails after the ceremony committed, the
ceremony is still burned and the device runs a fresh one. The failure mode is
"log in again", never "the assertion is still redeemable".

A **refused** exchange also burns the ceremony. That is why refusals are
returned as values from inside the owner command rather than raised: an
exception would roll the burn back and leave the row redeemable.

## Never provisioned, never guessed

An unbound subject is refused. There is no just-in-time provisioning, no
match-by-email, and no second lookup to fall back to — one query, one exact
`(binding, subject)` match on an active row, or a refusal. Binding a subject to
a party is an operator action with its own evidence
(`app/services/credential_party_binding.py`); a login is not the place to
invent an identity.

Subject uniqueness is a constraint, not a convention:
`ux_user_credentials_external_subject` (migration 560) is a partial unique
index over `(authentication_binding_id, username)` for non-local credentials.
The existing composite unique says a *party* holds at most one credential per
verifier; it says nothing about two parties both claiming subject `S`. The
resolver additionally refuses a multi-row match, because a resolver that would
pick arbitrarily if the index were dropped is a resolver that picks
arbitrarily.

## Bounded JWKS refresh

An unknown `kid` is a normal event (key rotation) and must be recoverable
without a restart. It is also a free amplifier: an unauthenticated caller
chooses the `kid`, and a resolver that refreshes whenever it does not recognise
one turns each such request into an outbound request to the identity provider.

The bound has three independent parts:

* **At most one fetch per resolution.** No retry loop. A failed refresh answers
  "no key" and the exchange refuses.
* **A minimum interval between *attempts*, not between successes.** The stamp
  moves before the fetch and moves whether it succeeds or fails. Stamping only
  on success would leave a permanently failing identity provider being polled
  by every request.
* **The working key set survives a failed refresh.** An outage does not
  invalidate keys that are still valid.

The cache is per process, deliberately not shared through Redis: a JWKS is
small, cheap to re-fetch and public, and sharing it would add a cross-process
write path and a cache-poisoning surface to protect a value that is already
verified by its own use.

## Observability, and what may never appear in it

Metrics (`app/metrics.py`): `oidc_mobile_ceremony_started_total`,
`oidc_mobile_ceremony_cancelled_total`, `oidc_mobile_ceremony_completed_total`,
`oidc_mobile_exchange_failed_total{reason}`,
`oidc_mobile_jwks_refresh_failures_total{stage}`,
`oidc_mobile_replay_refused_total{kind}`,
`oidc_mobile_unbound_subject_total`.

Events: `oidc_mobile_ceremony.started`, `oidc_mobile_assertion.admitted`
(schema version 1). Audit: `auth.oidc_mobile_assertion_admitted`.

`reason` comes from a **closed** vocabulary (`REFUSAL_REASONS`); a category
outside it cannot be emitted, which is what stops a future refusal carrying a
subject or a token fragment "just for debugging". Nothing on this path — log
line, metric label, event payload, audit record or error detail — carries a
token, an authorization code, a PKCE verifier, a nonce, an external subject, a
key id or an email, truncated or otherwise. A truncated identifier is still an
identifier to anyone holding the other half.

Every refusal maps to one status code and one message. Varying the code by
reason would restore exactly the enumeration oracle the single message removes:
a `404` for "no such ceremony" against a `403` for "disabled binding" tells a
prober which half of its guess was right. The distinction is real and is
recorded for the operator, not handed to the caller.

## Operator setup

1. Install an `authentication_bindings` row with `mechanism_code = "oidc"` and
   the `binding_key` the settings name. Two issuers are two bindings.
2. Bind each field technician's external subject: a `user_credentials` row with
   that `authentication_binding_id`, `username` = the subject, the staff
   `system_user_id`, and the party/tenant/evidence projection columns
   (`credential_party_binding` owns this write).
3. Configure the settings above.
4. Turn `auth.oidc_mobile_federation` on. The next boot verifies the
   configuration and refuses to start if any identifier is missing.

Deactivating the binding row disables the client: every exchange is then
refused with `verifier_unavailable`, and no session is issued.
