# `links.dotmac.io` — the field app's verified redirect boundary

> ## THIS REPOSITORY DOES NOT OWN THIS ORIGIN, AND THE ORIGIN DOES NOT EXIST YET
>
> `dotmac_sub` owns `selfcare.dotmac.io` (`nginx/selfcare.dotmac.io.conf`,
> `deploy/nginx/selfcare.dotmac.io`). `links.dotmac.io` is **fleet-owned**,
> deliberately **not** Sub's deployment hostname and **not** a tenant hostname,
> and **is not built**. Nothing in this directory is wired into any deployment
> automation, and nothing here should be: wiring it to a guessed host is how a
> redirect boundary ends up pointing somewhere nobody controls.
>
> This directory is the **content and the contract**. When the owner explicitly
> names the host, the serving configuration gets wired into that
> infrastructure's own path — a later step, by whoever owns it.

## What this is for

The field app's OIDC ceremony hands its authorization response back through one
permanent HTTPS URL:

```
https://links.dotmac.io/oidc/field/callback
```

The operating system — not the app, not the identity provider — decides whether
that URL opens the app, by fetching an **association document** from this origin
and comparing it against the installed app's real, signed identity. That is what
makes this a *verified* boundary and what a custom scheme
(`io.dotmac.field://…`) can never be: any app on the device can register a custom
scheme, so it delivers the authorization code to whoever registered it first.

Custom schemes and wildcard redirects are forbidden here. There is no fallback
scheme, deliberately.

## Contents

| Path | What it is |
| --- | --- |
| `site/.well-known/assetlinks.json` | Android App Links association document |
| `site/.well-known/apple-app-site-association` | iOS Universal Links association document (**no file extension**) |
| `site/oidc/field/callback.html` | Browser fallback page for the callback URL |
| `links.dotmac.io.conf` | nginx **serving specification** |
| `verify-origin.sh` | Read-only origin health check |

The offline, cross-artifact gate is `scripts/check_field_applinks.py` at the
repository root. It proves these documents agree with the Android intent filter,
the iOS entitlement, `field_mobile/brand.json` and the Dart defaults, and it is
what refuses an identity placeholder.

## The two unmet prerequisites

Both documents carry a **clearly-marked placeholder** where an identifier that
does not yet exist belongs. Neither may ever be published.

| Placeholder | What replaces it | Where it comes from |
| --- | --- | --- |
| `__ANDROID_CERT_SHA256__` | The SHA-256 of the **production app-signing certificate** | Play Console → *Setup* → *App integrity* → *App signing key certificate* |
| `__APPLE_TEAM_ID__` | The Apple Developer **Team ID** (10 alphanumeric characters) | Apple Developer portal → *Membership details* |

### Android — Play App Signing (the decided process)

The authoritative link identity is **the certificate that signs the application
users actually install**. Under Play App Signing that certificate is Google's,
not the local upload key's.

1. Register `io.dotmac.field` in Play Console.
2. Enrol in **Play App Signing**.
3. Take the **app-signing** certificate's SHA-256 from *Setup → App integrity*.
4. Put that fingerprint — and only that one — in `assetlinks.json`.
5. Deliver internal pilots through **Play Internal Testing**, so pilot builds are
   signed by the same app-signing certificate and verify against the same
   document.

**Do not** derive a fingerprint from the local upload keystore, and **do not**
list the upload key alongside the real one. Every accepted fingerprint authorizes
that signing identity to claim the link, so an unnecessary key widens the trust
surface for no benefit. `check_field_applinks.py` therefore requires
`sha256_cert_fingerprints` to hold **exactly one** entry; adding a second is a
deliberate, reviewed edit to the gate — justified only if APKs signed by another
key are genuinely distributed outside Play.

### iOS — the Team ID

The iOS project currently sets **no `DEVELOPMENT_TEAM`** at all. Once the Team ID
exists, it goes in two places: the `appIDs` entry in
`apple-app-site-association`, and `DEVELOPMENT_TEAM` for the `Runner` target.
Associated Domains also has to be enabled on the App ID, the way Push
Notifications already is; Xcode Cloud's managed signing provisions it from the
entitlement.

## Requirements the future host must satisfy

Each of these is checked by `verify-origin.sh` and has a line in
`field_mobile/docs/APP_LINKS_VERIFICATION.md`.

### DNS

* One `A`/`AAAA` (or `CNAME` to a fleet-owned target) for `links.dotmac.io`,
  resolvable from the public internet — Apple's CDN resolves it, not the device.
* **No wildcard record** that would let a sibling name answer for this one.
* The name is permanent. It is baked into shipped binaries' native declarations,
  so retiring it strands every installed app.

### TLS

* A publicly trusted, unexpired certificate, served with the **full chain**
  (an incomplete chain validates in a desktop browser and fails on device).
* TLS 1.2 minimum.
* **Renewal is a verification event.** Both verifiers fail closed and *silently*
  on a bad chain: the symptom is "links stopped opening the app", with no error
  anywhere. Run `verify-origin.sh` after every renewal.

### No redirection — the one that bites most often

Both verifiers follow **zero** redirects and read one as *"document absent"*.
So on `https://links.dotmac.io`, none of these may exist for the two
`.well-known` paths:

* `http` → `https` (fine on port 80; the verifiers are never pointed at `http://`)
* apex ↔ `www`
* trailing-slash normalisation
* a CDN or WAF "canonical host" rule
* an SPA catch-all rewrite

The serving spec uses `location =` (nginx exact match) for exactly this reason,
and the offline gate fails if a `return 30x` or `rewrite` appears in the TLS
server block.

### MIME types

| Path | Required `Content-Type` |
| --- | --- |
| `/.well-known/assetlinks.json` | `application/json` |
| `/.well-known/apple-app-site-association` | `application/json` |
| `/oidc/field/callback` | `text/html` |

The Apple document has **no file extension**, so nginx's `mime.types` cannot
infer anything and would serve `application/octet-stream`. The explicit
`default_type application/json` in the serving spec is what makes it correct.

### Access

No authentication, no IP allow-list, no geo-fence, no bot filter, no rate limit
in front of `/.well-known/`. Android fetches from the device; Apple fetches
through its own CDN from addresses that cannot be predicted.

### Cache policy

* Association documents: `Cache-Control: public, max-age=300`. Short on purpose —
  a fingerprint correction has to propagate in minutes, and neither Android's
  install-time verifier nor Apple's CDN offers a flush you can reach from here.
* Callback page: `Cache-Control: no-store`.
* Apple's CDN keeps its own copy regardless of these headers. Treat any change to
  the Apple document as taking **up to 24 hours** to reach devices, and re-run
  the device checks after it has.

### Health check

```sh
./verify-origin.sh                                  # the documented default origin
./verify-origin.sh https://links.staging.dotmac.io  # or an explicit one
```

It checks TLS validation, HTTP 200, **zero redirects**, `Content-Type`, JSON
validity, the browser fallback, and that **no identity placeholder was
published** — and it prints the Google Digital Asset Links API URL that shows
what Android itself sees.

## Verification commands

Android (both from a host with network access; the second is what the device does):

```sh
curl -sS -D - --max-redirs 0 https://links.dotmac.io/.well-known/assetlinks.json

curl -sS "https://digitalassetlinks.googleapis.com/v1/statements:list\
?source.web.site=https://links.dotmac.io\
&relation=delegate_permission/common.handle_all_urls"
```

On a connected device, with the signed build installed:

```sh
adb shell pm get-app-links io.dotmac.field          # want: links.dotmac.io: verified
adb shell am start -a android.intent.action.VIEW \
  -d "https://links.dotmac.io/oidc/field/callback?code=probe&state=probe"
```

iOS:

```sh
curl -sS -D - --max-redirs 0 \
  https://links.dotmac.io/.well-known/apple-app-site-association
```

On a connected device, Universal Links have no `adb` equivalent — open the URL
from Notes or Messages (**not** from Safari's address bar, which deliberately
does not follow a Universal Link to the app). Device-side association traffic is
visible in Console.app under the `swcd` subsystem.

The full, ordered device procedure — including the negative cases that matter
most (wrong path, wrong host, unsigned build) — is
`field_mobile/docs/APP_LINKS_VERIFICATION.md`, to be executed in Wave 10.

## Changing the origin or the path

Five files move together, and the gate fails until they agree:

1. `field_mobile/lib/core/deeplink/oidc_redirect.dart` (the Dart defaults)
2. `field_mobile/brand.json` (`OIDC_CALLBACK_ORIGIN`, `OIDC_CALLBACK_PATH`)
3. `field_mobile/android/app/src/main/AndroidManifest.xml` (the intent filter)
4. `field_mobile/ios/Runner/Runner.entitlements` (the associated domain)
5. `site/.well-known/*` here, plus `links.dotmac.io.conf`

…and the new `redirect_uri` must be registered verbatim with the identity
provider. An origin change also invalidates every installed build: the native
declarations are compiled in, so old installs keep pointing at the old origin
until they update.
