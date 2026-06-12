# Push notifications (FCM) — enablement

The mobile FCM client is fully wired. It is **inert until the platform config
files are dropped in** — without them the app builds and runs normally with
push disabled (the in-app notification inbox still works). No code change is
needed to "light it up".

## What the app already does

- `lib/src/core/push_service.dart` — Firebase init (guarded), OS permission
  request (Android 13 runtime / iOS), foreground display via
  `flutter_local_notifications`, FCM token retrieval + `onTokenRefresh`.
- `lib/src/repositories/push_repository.dart` — `POST /me/push-tokens` on
  login, `DELETE /me/push-tokens/{token}` on logout.
- `auth_controller` registers the device after `/auth/me` (login + cold-start)
  and de-registers on logout; `main.dart` registers the background handler.
- Android: `POST_NOTIFICATIONS` permission + default-channel metadata; the
  `com.google.gms.google-services` plugin is applied **only when**
  `android/app/google-services.json` exists.
- iOS: `UIBackgroundModes: remote-notification` + `Runner.entitlements`
  (`aps-environment`).

## To enable (operator)

1. **Firebase project** → add an Android app (applicationId `io.dotmac.selfcare`)
   and an iOS app, then download:
   - `google-services.json` → `mobile/android/app/google-services.json`
   - `GoogleService-Info.plist` → `mobile/ios/Runner/GoogleService-Info.plist`
   (Both are gitignored.)
2. **iOS only**, in Xcode (`ios/Runner.xcworkspace`): select the Runner target →
   Signing & Capabilities → **+ Capability → Push Notifications**. This wires
   `Runner.entitlements` into the target. Add `GoogleService-Info.plist` to the
   Runner target in Xcode (so it's bundled). Upload an **APNs auth key (.p8)**
   to Firebase → Project settings → Cloud Messaging → Apple app config.
3. **Backend** (server send): set in the prod `.env` and rebuild/redeploy app +
   celery workers:
   - `FCM_PROJECT_ID=<your-firebase-project-id>`
   - `FCM_CREDENTIALS_JSON='<the service-account JSON>'`
     (Firebase → Project settings → Service accounts → Generate new private key)
     or `GOOGLE_APPLICATION_CREDENTIALS=/path/to/that.json`.
   `google-auth` is already a dependency.
4. Enable the notification queue runner (`notification.notification_queue_enabled`)
   **after** triaging the stale backlog — see the notifications stack notes.

## Deterministic device test (no real customers)

- **Usage alert**: give a test subscription a quota bucket ≥80% used (or
  temporarily set `usage.usage_warning_thresholds=0.05`, revert after), then
  `celery -A app.celery_app call app.tasks.usage.evaluate_fup_rules`.
- **Bundle expiry**: create a test add-on with `end_at = now + 2h`, then
  `celery -A app.celery_app call app.tasks.usage.notify_expiring_data_bundles`.
- Verify the `notifications` row (channel=push) goes queued → delivered, the
  worker log shows the FCM 200, and the device shows the banner in both
  foreground and background.
