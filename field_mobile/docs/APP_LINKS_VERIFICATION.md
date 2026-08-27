# App Links / Universal Links — device verification checklist

**Execute in Wave 10.** Everything here needs a *signed* artifact, a *live*
origin and a *physical* device, so none of it can be automated, run in CI, or run
on a workstation. Tick each box with the observed output pasted beside it; an
unticked box is an unverified boundary, not a formality.

The offline, automatable half already runs in CI
(`scripts/check_field_applinks.py`) and proves the six artifacts agree with each
other. This document proves the *operating system agrees with them* — which is
the only claim that matters, and the only one a repository cannot make about
itself.

## Why every failure here is silent

There is no error path. A wrong fingerprint, an expired certificate, a single
redirect, an `application/octet-stream` content type, an entitlement the target
did not sign with — each produces exactly one symptom: **the link opens the
browser instead of the app.** Nothing is logged, no exception is raised, and the
app behaves normally in every other respect. That is why this is a checklist and
not an assumption.

---

## 0. Prerequisites — do not start until all four are true

- [ ] `https://links.dotmac.io` exists, serves both association documents, and
      `deploy/links.dotmac.io/verify-origin.sh` passes with **zero** failures.
- [ ] `assetlinks.json` carries the **production app-signing** certificate
      SHA-256 from Play Console → *Setup* → *App integrity* — **not** the local
      upload key's fingerprint. Under Play App Signing the production certificate
      is Google's; substituting the upload key breaks verification silently.
- [ ] `apple-app-site-association` carries the real Apple **Team ID**, and the
      `Runner` target's `DEVELOPMENT_TEAM` is set to the same value with
      *Associated Domains* enabled on the App ID.
- [ ] `scripts/check_field_applinks.py --require-real` exits 0. If it does not, a
      placeholder or a malformed identifier is still present and **nothing below
      can pass**.

Record the two identifiers here when they exist (both are public identifiers, not
secrets — but never record a keystore password anywhere):

| Identifier | Value | Source |
| --- | --- | --- |
| Android app-signing SHA-256 | | Play Console → Setup → App integrity |
| Apple Team ID | | Developer portal → Membership details |

---

## 1. Origin — from any host with network access

- [ ] **1.1 `assetlinks.json` is served correctly, with no redirect**

      curl -sS -D - --max-redirs 0 https://links.dotmac.io/.well-known/assetlinks.json

      Expect `HTTP/2 200`, `content-type: application/json`, and the document
      body. A `301`/`302` here is a **fail**: Android's verifier follows none.

- [ ] **1.2 `apple-app-site-association` is served correctly, with no redirect**

      curl -sS -D - --max-redirs 0 \
        https://links.dotmac.io/.well-known/apple-app-site-association

      Expect `HTTP/2 200` and `content-type: application/json`. The file has no
      extension, so `application/octet-stream` here is a **fail**.

- [ ] **1.3 The certificate chain is complete and publicly trusted**

      openssl s_client -connect links.dotmac.io:443 -servername links.dotmac.io \
        </dev/null 2>/dev/null | openssl x509 -noout -dates -issuer

      A chain that validates in a desktop browser but is missing an
      intermediate will still fail on device.

- [ ] **1.4 Google's verifier agrees** (this is literally what Android asks)

      curl -sS "https://digitalassetlinks.googleapis.com/v1/statements:list\
      ?source.web.site=https://links.dotmac.io\
      &relation=delegate_permission/common.handle_all_urls"

      Expect one statement naming `io.dotmac.field` with the production
      fingerprint, and `"debugString"` free of errors.

- [ ] **1.5 Neither published document contains a placeholder**

      curl -sS https://links.dotmac.io/.well-known/assetlinks.json \
        | grep -c '__ANDROID_CERT_SHA256__'      # expect 0
      curl -sS https://links.dotmac.io/.well-known/apple-app-site-association \
        | grep -c '__APPLE_TEAM_ID__'            # expect 0

---

## 2. Android — signed build on a physical device

Install the **Play-signed** build (Internal Testing track), not a local
`flutter run` build. A locally signed build is signed by a different certificate
and **must not** verify — see §4.3, where that is the assertion.

- [ ] **2.1 The link is verified** — the single most important line

      adb shell pm get-app-links io.dotmac.field

      Expect, under `Domain verification state`:

          links.dotmac.io: verified

      `legacy_failure`, `1024` (none), or `unverified` is a **fail**. Force a
      re-check with:

          adb shell pm verify-app-links --re-verify io.dotmac.field

      Verification runs at install time and needs network. A device that was
      offline during install shows `unverified` until re-verified.

- [ ] **2.2 The exact callback path opens the app**

      adb shell am start -a android.intent.action.VIEW \
        -d "https://links.dotmac.io/oidc/field/callback?code=probe&state=probe"

      Expect the app to come to the foreground. Expect **no** browser and **no**
      disambiguation chooser — a chooser means the link is unverified.

- [ ] **2.3 A real tap, from outside the app**

      Send the URL to the device (Gmail, Messages, a note) and tap it with the
      app **backgrounded**. Same result: the app opens directly.

- [ ] **2.4 The probe callback is discarded, not processed**

      §2.2 and §2.3 arrive with no ceremony in flight. Expect the app to land on
      its normal screen and **not** attempt a sign-in, and expect no crash. This
      is `OidcCallbackOutcome.noActiveCeremony` — the unit tests cover the logic;
      this confirms the real intent path reaches it.

- [ ] **2.5 A genuine ceremony completes end to end**

      Start sign-in in the app, complete it at the identity provider, and confirm
      the app is returned to and signs in. Confirm the browser/custom tab closes
      or is left behind rather than showing the fallback page.

---

## 3. iOS — signed build on a physical device

Install via TestFlight. Universal Links do **not** work in the Simulator in any
way you can trust, and a development build without the Associated Domains
entitlement provisioned will silently fall back to Safari.

- [ ] **3.1 The device fetched and accepted the association**

      Connect the device to a Mac, open **Console.app**, select the device,
      filter on subsystem `com.apple.swcd` (or process `swcd`), then delete and
      reinstall the app. Expect a successful fetch of
      `https://links.dotmac.io/.well-known/apple-app-site-association` and no
      parse or entitlement error.

- [ ] **3.2 The exact callback path opens the app**

      Put the URL in **Notes** or **Messages** and tap it.

          https://links.dotmac.io/oidc/field/callback?code=probe&state=probe

      Expect the app to open. **Do not test by typing the URL into Safari's
      address bar** — Safari deliberately does not follow a Universal Link to the
      app from the address bar, so that is not a failure.

- [ ] **3.3 The probe callback is discarded, not processed** — as §2.4.

- [ ] **3.4 A genuine ceremony completes end to end** — as §2.5.

- [ ] **3.5 The app declares no custom scheme**

      Open the installed app's `Info.plist` from the `.ipa` payload and confirm
      **no** `CFBundleURLTypes` key. A custom scheme is claimable by any app on
      the device and is exactly what this boundary replaces.

---

## 4. The negative cases — these are the point of the exercise

A boundary that accepts the right link proves very little. A boundary that
*refuses* the wrong ones is the property being shipped.

- [ ] **4.1 A wrong PATH does not open the app**

      adb shell am start -a android.intent.action.VIEW \
        -d "https://links.dotmac.io/oidc/field/callback/extra?code=probe"
      adb shell am start -a android.intent.action.VIEW \
        -d "https://links.dotmac.io/oidc/field/callbackX?code=probe"
      adb shell am start -a android.intent.action.VIEW \
        -d "https://links.dotmac.io/?code=probe"

      Expect **the browser** every time. The app opening here would mean the
      intent filter is matching a prefix rather than the exact path.

      iOS: same three URLs from Notes. Expect Safari.

- [ ] **4.2 A wrong HOST does not open the app**

      adb shell am start -a android.intent.action.VIEW \
        -d "https://selfcare.dotmac.io/oidc/field/callback?code=probe"
      adb shell am start -a android.intent.action.VIEW \
        -d "https://links.dotmac.io.example.com/oidc/field/callback?code=probe"

      Expect the browser. The second is the look-alike suffix that a naive
      `endsWith` host check would accept.

      iOS: same two URLs from Notes. Expect Safari.

- [ ] **4.3 An UNSIGNED / differently-signed build does not open the link**

      Install a local `flutter build apk --release` build (signed with the debug
      or upload key, i.e. **not** the production app-signing certificate) on a
      clean device, then:

          adb shell pm get-app-links io.dotmac.field

      Expect **not** `verified`, and expect §2.2 to open the browser. This is the
      assertion that the fingerprint in `assetlinks.json` is doing real work: if
      an arbitrarily signed build *does* open the link, the document is wrong or
      the fingerprint is not being checked, and the boundary is worthless.

      Reinstall the Play-signed build afterwards.

- [ ] **4.4 The browser fallback is safe when the app is ABSENT**

      Uninstall the app. Open
      `https://links.dotmac.io/oidc/field/callback?code=probe&state=probe`
      in a mobile browser.

      Expect the static fallback page. Expect it to carry **no script**, to make
      **no** attempt to hand the query string to any app, and to offer **no**
      custom-scheme link. Confirm with *View source*.

- [ ] **4.5 There is still no custom scheme anywhere**

      adb shell dumpsys package io.dotmac.field | grep -A2 'Schemes:'

      Expect `https` and nothing else. No `io.dotmac.field://`, no
      `dotmacfield://`, not even as a fallback.

---

## 5. After any change to the origin

Re-run §1 in full, then §2.1 and §3.1, then §4.

- A **certificate renewal** is a change: an incomplete chain fails both verifiers
  silently.
- Apple's CDN caches the association document independently of the
  `Cache-Control` header. Allow **up to 24 hours** after editing
  `apple-app-site-association` before treating a §3 failure as real.
- Android re-verifies at install/update. `adb shell pm verify-app-links
  --re-verify io.dotmac.field` forces it without a reinstall.

---

## Sign-off

| Item | Result | Device / build | Date | Who |
| --- | --- | --- | --- | --- |
| §1 Origin | | | | |
| §2 Android verified + launch | | | | |
| §3 iOS association + launch | | | | |
| §4.1 Wrong path refused | | | | |
| §4.2 Wrong host refused | | | | |
| §4.3 Unsigned build refused | | | | |
| §4.4 Browser fallback safe | | | | |
