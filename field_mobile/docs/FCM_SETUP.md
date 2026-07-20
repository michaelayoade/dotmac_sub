# Enabling Push Notifications (FCM)

The Dart side is **fully wired**: `firebase_core` + `firebase_messaging` are in
`pubspec.yaml`, `lib/core/push/fcm_push_source.dart` implements the `PushSource`
interface, and `main.dart` initializes it via `FcmPushSource.tryCreate()` and
overrides `pushSourceProvider` **only when Firebase is configured**. Until then
the app falls back to `NoopPushSource`, so it builds and runs untouched.

Backend + registration are already in place: `PushRegistrar` registers the token
with `POST /api/v1/field/devices`, deep-links taps via `routeForMessage`, and the
server FCM sender lives in `app/services/push.py`.

## What remains (needs your Firebase project — credential-bearing)

1. **Generate native config** with the FlutterFire CLI. This produces
   `android/app/google-services.json`, `ios/Runner/GoogleService-Info.plist`, and
   `lib/firebase_options.dart` — the pieces only your Firebase account can produce:
   ```bash
   dart pub global activate flutterfire_cli
   flutterfire configure
   ```
   (App ids: Android `io.dotmac.field`, iOS bundle id to match.)
   `FcmPushSource.tryCreate()` calls `Firebase.initializeApp()` with no options,
   so it resolves from these native files — no code change needed after this.
   The Android `google-services` Gradle plugin is **already wired** (declared in
   `settings.gradle.kts`, applied conditionally in `app/build.gradle.kts` when the
   JSON is present), so you don't need to let `flutterfire configure` edit Gradle —
   if it does, keep the conditional version. See [RELEASE.md](RELEASE.md) for the
   CI path that injects these files from secrets instead of committing them.

2. **Android minSdk**: ensure >= 23 (firebase_core 3.x). The app currently uses
   `flutter.minSdkVersion`; bump in `android/app/build.gradle.kts` if needed.

3. **iOS**: upload your APNs auth key (`.p8`) to Firebase -> Project Settings ->
   Cloud Messaging, and enable **Push Notifications** + **Background Modes ->
   Remote notifications** capabilities in Xcode. (Deployment target is already
   iOS 13.)

4. **Backend**: set `FCM_SERVICE_ACCOUNT_JSON` (+ optional `FCM_PROJECT_ID`) on
   the API — see `app/services/push.py`. Until set, sends are recorded as failed
   deliveries.

## Optional: background data messages
`FcmPushSource` handles foreground messages and notification taps. If you need to
process **data-only** messages while the app is terminated/backgrounded, add a
top-level `@pragma('vm:entry-point')` handler and register it with
`FirebaseMessaging.onBackgroundMessage(...)` in `main()`. System notifications
already display without it.

## Verifying
- `flutter run`, log in -> confirm a `DeviceToken` row server-side.
- Assign a work order to the logged-in tech -> `queue_work_order_assignment_push`
  -> device receives "New job assigned" -> tapping it deep-links to `/jobs/{id}`.
