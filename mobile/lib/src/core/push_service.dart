import 'dart:io' show Platform;

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:go_router/go_router.dart';

import '../router/app_router.dart' show rootNavigatorKey;
import 'observability.dart';

/// Route a tapped notification to its target screen. Chat replies carry
/// `type: chat_message` (see crm_webhooks.send_push) — open the chat in the
/// Support window. Other types fall through to the notifications inbox.
void handleNotificationTap(Map<String, dynamic> data) {
  final ctx = rootNavigatorKey.currentContext;
  if (ctx == null) return;
  switch (data['type']) {
    case 'chat_message':
      ctx.go('/support/chat');
      break;
    default:
      ctx.go('/dashboard/notifications');
  }
}

/// Background isolate handler for data/notification messages. Must be a
/// top-level (or static) function annotated for the Flutter entrypoint.
@pragma('vm:entry-point')
Future<void> firebaseMessagingBackgroundHandler(RemoteMessage message) async {
  // The OS renders `notification`-type messages itself while backgrounded; we
  // only need a handler registered so data messages wake the isolate. Keep it
  // light — no Firebase API calls beyond what initializeApp set up.
  await Firebase.initializeApp();
}

/// Mobile push (FCM) client.
///
/// Entirely best-effort: every entry point is guarded so a build WITHOUT the
/// platform config (android/app/google-services.json /
/// ios/Runner/GoogleService-Info.plist) runs with push simply disabled — the
/// in-app notification inbox keeps working. The backend send path is likewise
/// a no-op until FCM server credentials are configured, so the two halves can
/// be enabled independently.
class PushService {
  PushService(
      {FirebaseMessaging? messaging, FlutterLocalNotificationsPlugin? local})
      : _messaging = messaging,
        _local = local ?? FlutterLocalNotificationsPlugin();

  FirebaseMessaging? _messaging;
  final FlutterLocalNotificationsPlugin _local;

  bool _available = false;
  bool _handlersWired = false;

  /// True once Firebase initialised successfully and FCM is usable.
  bool get isAvailable => _available;

  static const _androidChannel = AndroidNotificationChannel(
    'default_high_importance',
    'Notifications',
    description: 'Account, billing and usage alerts',
    importance: Importance.high,
  );

  /// Initialise Firebase + local-notification plumbing. Safe to call once at
  /// startup; returns false (push disabled) when no platform config is present.
  Future<bool> init() async {
    // Idempotent: safe to call on every login / bootstrap.
    if (_available) return true;
    try {
      if (Firebase.apps.isEmpty) {
        await Firebase.initializeApp();
      }
    } catch (e) {
      // No google-services.json / GoogleService-Info.plist, or Firebase not
      // set up for this build — run with push disabled.
      Log.breadcrumb('push: firebase init skipped',
          category: 'push', data: {'error': '$e'});
      _available = false;
      return false;
    }

    _messaging ??= FirebaseMessaging.instance;
    try {
      const androidInit = AndroidInitializationSettings('@mipmap/ic_launcher');
      const iosInit = DarwinInitializationSettings(
        requestAlertPermission: false,
        requestBadgePermission: false,
        requestSoundPermission: false,
      );
      await _local.initialize(
        const InitializationSettings(android: androidInit, iOS: iosInit),
      );
      await _local
          .resolvePlatformSpecificImplementation<
              AndroidFlutterLocalNotificationsPlugin>()
          ?.createNotificationChannel(_androidChannel);
      _available = true;
      return true;
    } catch (e) {
      Log.breadcrumb('push: local-notification init failed',
          category: 'push', data: {'error': '$e'});
      _available = false;
      return false;
    }
  }

  /// Request the OS notification permission (Android 13+ runtime prompt; iOS
  /// explicit). No-op when push is unavailable. Returns true if authorised.
  Future<bool> requestPermission() async {
    final messaging = _messaging;
    if (!_available || messaging == null) return false;
    try {
      final settings = await messaging.requestPermission();
      return settings.authorizationStatus == AuthorizationStatus.authorized ||
          settings.authorizationStatus == AuthorizationStatus.provisional;
    } catch (e) {
      Log.breadcrumb('push: permission request failed',
          category: 'push', data: {'error': '$e'});
      return false;
    }
  }

  /// Wire foreground display + token-refresh re-registration. Idempotent.
  void wireForegroundHandlers(Future<void> Function(String token) onToken) {
    final messaging = _messaging;
    if (!_available || messaging == null || _handlersWired) return;
    _handlersWired = true;
    FirebaseMessaging.onMessage.listen(_showForeground);
    // Tap routing: backgrounded tap, and the launch message on a cold start.
    FirebaseMessaging.onMessageOpenedApp
        .listen((m) => handleNotificationTap(m.data));
    messaging.getInitialMessage().then((m) {
      if (m != null) handleNotificationTap(m.data);
    });
    messaging.onTokenRefresh.listen((token) {
      onToken(token).catchError((Object e) {
        Log.breadcrumb('push: token refresh re-register failed',
            category: 'push', data: {'error': '$e'});
      });
    });
  }

  Future<void> _showForeground(RemoteMessage message) async {
    final n = message.notification;
    if (n == null) return;
    try {
      await _local.show(
        n.hashCode,
        n.title,
        n.body,
        NotificationDetails(
          android: AndroidNotificationDetails(
            _androidChannel.id,
            _androidChannel.name,
            channelDescription: _androidChannel.description,
            importance: Importance.high,
            priority: Priority.high,
          ),
          iOS: const DarwinNotificationDetails(),
        ),
      );
    } catch (e) {
      Log.breadcrumb('push: foreground display failed',
          category: 'push', data: {'error': '$e'});
    }
  }

  /// The current device FCM token, or null when unavailable.
  Future<String?> currentToken() async {
    final messaging = _messaging;
    if (!_available || messaging == null) return null;
    try {
      // iOS: getToken() returns null until the APNs token is set, and after a
      // fresh notification-permission grant the APNs token takes a moment to
      // arrive. Wait for it (briefly) before requesting the FCM token —
      // otherwise registration silently no-ops on iOS. Android needs no APNs.
      if (Platform.isIOS) {
        var apns = await messaging.getAPNSToken();
        for (var i = 0; apns == null && i < 5; i++) {
          await Future<void>.delayed(const Duration(seconds: 1));
          apns = await messaging.getAPNSToken();
        }
        if (apns == null) {
          Log.breadcrumb('push: APNs token unavailable; skipping getToken',
              category: 'push');
          return null;
        }
      }
      return await messaging.getToken();
    } catch (e) {
      Log.breadcrumb('push: getToken failed',
          category: 'push', data: {'error': '$e'});
      return null;
    }
  }

  /// Best-effort: delete the device token from FCM (used on logout, after the
  /// backend de-registration call).
  Future<void> deleteToken() async {
    final messaging = _messaging;
    if (!_available || messaging == null) return;
    try {
      await messaging.deleteToken();
    } catch (e) {
      Log.breadcrumb('push: deleteToken failed',
          category: 'push', data: {'error': '$e'});
    }
  }

  /// Platform tag stored alongside the token server-side.
  static String platformTag() {
    if (kIsWeb) return 'web';
    if (Platform.isAndroid) return 'android';
    if (Platform.isIOS) return 'ios';
    return 'unknown';
  }
}
