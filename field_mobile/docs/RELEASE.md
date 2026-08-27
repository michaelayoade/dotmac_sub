# Field App — Release Pipeline

Signed release builds for the **dotmac_field** technician app.
Push/FCM setup is in [FCM_SETUP.md](FCM_SETUP.md); this doc covers shipping.

- **Android** → GitHub Actions ([`.github/workflows/field-app-release.yml`](../../.github/workflows/field-app-release.yml)) → signed `.aab`/`.apk` artifact
- **iOS** → Xcode Cloud (`ios/ci_scripts/ci_post_clone.sh`) → TestFlight

The pipeline is wired and its own preflight job proves the wiring on every pull
request. It still needs credentials + store records (below) before it can ship.

App identifiers — **distinct from the customer selfcare app on purpose**, and
asserted by CI rather than assumed:

| | field app | customer app |
|---|---|---|
| Android `applicationId` | `io.dotmac.field` | `io.dotmac.selfcare` |
| iOS bundle id | `io.dotmac.field` | `io.dotmac.selfcare` |
| release workflow | `field-app-release.yml` | `mobile-release.yml` |
| brand config | `field_mobile/brand.json` | repository-root `brand.json` |
| Play / App Store record | separate | separate |

The two release paths are structurally disjoint: neither workflow, nor either
app's Xcode Cloud hook, may name the other's project directory. The preflight
job fails the build if one starts to, and the Android job additionally reads the
application id out of the produced artifact.

---

## Configuration (no hardcoded values)

Every environment-specific value in the pipeline is a knob with a documented
default, declared exactly once in the workflow's top-level `env:` block. Override
any of them per repository under **Settings → Secrets and variables → Actions →
Variables** — no workflow edit required.

| Repository variable | Default | What it controls |
|---|---|---|
| `FIELD_FLUTTER_VERSION` | `3.44.1` | Flutter SDK the workflow installs (also a per-run `workflow_dispatch` input) |
| `FIELD_FLUTTER_CHANNEL` | `stable` | Flutter channel |
| `FIELD_JAVA_VERSION` | `17` | JDK for the Gradle build |
| `FIELD_JAVA_DISTRIBUTION` | `temurin` | JDK vendor |
| `FIELD_ANDROID_BUILD_TOOLS_VERSION` | `35.0.0` | build-tools supplying `apksigner`/`aapt2` for artifact verification; installed on demand if absent |
| `FIELD_APPLICATION_ID` | `io.dotmac.field` | expected Android application id (asserted against the artifact) |
| `FIELD_BUNDLE_ID` | `io.dotmac.field` | expected iOS bundle id (asserted against the built `.app`) |
| `FIELD_API_BASE_URL` | *(unset → the value in `brand.json`)* | backend override for a build |
| `FIELD_ALLOW_FLUTTER_REVISION_DRIFT` | `0` | escape hatch while bumping Flutter |

**The Flutter pin is single-sourced.** `field_mobile/.metadata` records the SDK
revision the project tracks, and `ios/ci_scripts/ci_post_clone.sh` derives its
Xcode Cloud pin from that file — no literal version in the script. The GitHub
workflow installs `FIELD_FLUTTER_VERSION` and then **fails if the installed
revision differs from `.metadata`**, so the two cannot drift apart unnoticed.
Bumping Flutter means updating both; set `FIELD_ALLOW_FLUTTER_REVISION_DRIFT=1`
for the transition.

**Brand configuration** lives in `field_mobile/brand.json` and is passed to every
build as `--dart-define-from-file`. It is the field app's own file, separate from
the repository-root `brand.json` that belongs to the customer app. Preflight
enforces both directions of parity with the Dart code: a key in `brand.json` that
nothing reads fails the build, and a `BRAND_*` key read by `field_mobile/lib`
that `brand.json` omits fails the build — which is what stops a release from
silently shipping Flutter's default theme. A missing brand file is a hard error
in both the Android workflow and the iOS hook, never a warning.

---

## Android

### One-time setup
1. **Generate an upload keystore** (Play requires a fresh key). The field app is
   a **separate Play record** from the customer app, so it needs its **own**
   upload key — do not reuse the customer app's:
   ```bash
   keytool -genkey -v -keystore field-upload-keystore.jks -keyalg RSA -keysize 2048 \
     -validity 10000 -alias upload
   ```
   Back this file up — losing it before Play App Signing enrollment means you can
   never update the app.
2. **Add GitHub repository secrets** (Settings → Secrets and variables → Actions
   → Secrets). The first four are **required**; the build refuses to start
   without them, naming the exact missing secret, rather than falling back to the
   debug key.

   | Secret | Required | Value |
   |---|---|---|
   | `FIELD_ANDROID_KEYSTORE_BASE64` | yes | `base64 -i field-upload-keystore.jks` |
   | `FIELD_ANDROID_KEYSTORE_PASSWORD` | yes | keystore password |
   | `FIELD_ANDROID_KEY_ALIAS` | yes | `upload` |
   | `FIELD_ANDROID_KEY_PASSWORD` | yes | key password |
   | `FIELD_ANDROID_GOOGLE_SERVICES_JSON_B64` | no | `base64 -i google-services.json` — enables FCM; without it the build succeeds with push disabled |
   | `FIELD_SENTRY_DSN` | no | Sentry DSN for crash reporting; without it telemetry stays off |

   No secret value is ever echoed by the workflow. Presence is reported as
   `secret NAME: present`, and the only cryptographic material printed is
   certificate fingerprints, which are public.

### Build
- Manual: **Actions → Field App Release → Run workflow** (choose `appbundle` or
  `apk`; optionally override the Flutter version or the API base URL for that run)
- Tag: push `field-mobile-v1.0.1` → builds automatically. The tag must match
  `version:` in `pubspec.yaml`, which is the version source (Gradle reads
  `flutter.versionName` / `flutter.versionCode` from it); a mismatch fails
  preflight.

The signed artifact `dotmac-field-android-release` is attached to the run,
together with a `SHA256SUMS.txt` covering it.

### What the workflow verifies before it will hand you an artifact
- **The checkout is clean** and carries no leftover `key.properties`,
  `release.jks` or `google-services.json`.
- **Every required secret is present** — failing with the exact missing name.
- **The keystore actually opens** with the supplied password and contains the
  supplied alias.
- **The artifact's signing certificate is the upload key.** The certificate is
  read out of the produced artifact (`keytool -printcert -jarfile` for an `.aab`,
  `apksigner verify --print-certs` for an `.apk`), and its SHA-256 fingerprint
  must equal the fingerprint of the key restored from
  `FIELD_ANDROID_KEYSTORE_BASE64`. A certificate whose subject is the Android
  debug identity is rejected outright. *(The previous check only asserted that a
  file existed — it would have passed a debug-signed build.)*
- **The application id in the artifact is `io.dotmac.field`** and is not the
  customer app's, so a crossed pipeline cannot ship.
- Signing material is deleted from the runner in an `always()` step.

### Signing plumbing (already in the repo)
- `android/app/build.gradle.kts` reads `android/key.properties`; without it,
  release builds fall back to the debug key so local `flutter run --release`
  still works. That fallback is exactly why CI verifies the signature rather than
  trusting the build to have used the right key.
- The `com.google.gms.google-services` plugin is declared in
  `android/settings.gradle.kts` and **applied conditionally** — only when
  `android/app/google-services.json` exists. So you don't need
  `flutterfire configure` to touch Gradle; just drop the JSON in (or provide the
  `FIELD_ANDROID_GOOGLE_SERVICES_JSON_B64` secret) and FCM turns on.
- `android/key.properties`, `android/app/release.jks` and
  `android/app/google-services.json` are gitignored; CI materializes them and
  removes them again.

### Play Console
The app is a fresh publish (no prior listing). You'll need: store listing, content
rating, Data safety form, target audience, countries, and — for a new personal
account — a 12-tester / 14-day closed test before production. **Store upload is
deliberately not part of this workflow**; it produces and verifies an artifact,
nothing more.

---

## iOS (Xcode Cloud → TestFlight)

iOS release archives are built by **Xcode Cloud**, not GitHub Actions. Xcode Cloud
owns Apple signing via managed certificates, and store submission stays manual.

`ios/ci_scripts/ci_post_clone.sh` is the hook Xcode Cloud runs after cloning. It
installs Flutter pinned to `field_mobile/.metadata`, runs codegen, and builds
**field_mobile** with the field brand file.

> It used to point at the customer app's directory, so the field app's Xcode
> Cloud workflow installed Flutter and then built and archived the *selfcare*
> app. That is fixed, and two things now keep it fixed: the script asserts the
> bundle id of the app it produced, and the `iOS build (no codesign)` job in
> `field-app-release.yml` runs this exact script on a macOS runner on every pull
> request that touches it.

### One-time setup
1. **App Store Connect**: create an app record for bundle id `io.dotmac.field`.
2. **Xcode Cloud**: create a workflow on `ios/Runner.xcworkspace`.
   `ios/ci_scripts/ci_post_clone.sh` auto-runs after clone and bootstraps
   everything the build needs.
3. **Post-Actions**: add **TestFlight Internal Testing** and select your tester
   group so builds attach automatically (otherwise each build must be added by
   hand).

### Xcode Cloud environment variables
All optional; each has a documented default.

| Variable | Default | Value |
|---|---|---|
| `API_BASE_URL` | the value in `field_mobile/brand.json` | backend base URL for this build |
| `SENTRY_DSN` | *(unset → telemetry off)* | crash reporting |
| `GOOGLE_SERVICE_INFO_PLIST_B64` | *(unset → push disabled)* | `base64 -i GoogleService-Info.plist` |
| `FIELD_APP_DIR` | `<repo>/field_mobile` | Flutter project directory |
| `FIELD_BRAND_FILE` | `$FIELD_APP_DIR/brand.json` | brand config passed as `--dart-define-from-file` |
| `FLUTTER_REVISION` | the revision in `field_mobile/.metadata` | Flutter SDK pin |
| `FLUTTER_GIT_URL` | upstream `flutter/flutter` | SDK source (mirrors) |

When `GOOGLE_SERVICE_INFO_PLIST_B64` is set, the hook materializes the plist,
flips `Runner.entitlements` to `aps-environment: production`, and runs
`ios/ci_scripts/wire_firebase.rb` to bundle the plist and attach the entitlement
to the Runner target. Without it the app builds with push disabled
(`NoopPushSource`).

Also upload your APNs auth key (`.p8`) to Firebase → Project Settings → Cloud
Messaging so the server can deliver to iOS.

---

## Screenshots (store listings)

The listing copy lives in [store_listing.md](store_listing.md). Screenshots are
captured by an integration-test harness that logs in and shoots each primary tab:

```bash
DEMO_USERNAME=tech@example.com DEMO_PASSWORD=secret tool/screenshots.sh -d <device-id>
```

- Output → `build/screenshots/` (`01_today.png` … `05_customers.png`)
- App Store needs a **6.9" iPhone** (1320×2868) and a **13" iPad**; Play needs a
  phone + tablet. Boot the matching simulator/emulator and run once per device
  (`flutter devices` for ids).
- Needs a **working technician demo account** (the same one App Review requires).
- The harness reuses the production bootstrap via `buildFieldAppRoot()` in
  `lib/main.dart`, so screens render exactly as shipped. Files:
  `integration_test/screenshots_test.dart`, `test_driver/screenshot_driver.dart`.

---

## What's still yours to provide (not code)
1. Firebase project → `google-services.json` + `GoogleService-Info.plist` + backend
   `FCM_SERVICE_ACCOUNT_JSON` / `FCM_PROJECT_ID` (see [FCM_SETUP.md](FCM_SETUP.md))
2. A **field-specific** Android upload keystore → the four required
   `FIELD_ANDROID_*` GitHub secrets above
3. Store records: App Store Connect app + Play Console listing for
   `io.dotmac.field`
