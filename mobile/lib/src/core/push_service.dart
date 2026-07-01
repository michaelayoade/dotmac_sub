import 'dart:async';
import 'dart:convert';
import 'dart:io' show Platform;

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import 'observability.dart';

typedef PushRouteHandler = FutureOr<void> Function(String route);

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
  bool _tapHandlersWired = false;
  bool _initialMessageChecked = false;
  PushRouteHandler? _routeHandler;

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
        onDidReceiveNotificationResponse: (response) =>
            _handleLocalPayload(response.payload),
      );
      await _local
          .resolvePlatformSpecificImplementation<
              AndroidFlutterLocalNotificationsPlugin>()
          ?.createNotificationChannel(_androidChannel);
      _available = true;
      _wireTapHandlers();
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
    messaging.onTokenRefresh.listen((token) {
      onToken(token).catchError((Object e) {
        Log.breadcrumb('push: token refresh re-register failed',
            category: 'push', data: {'error': '$e'});
      });
    });
  }

  /// Wire notification taps to app routes. Safe to call before Firebase is
  /// available; [init] completes the native listeners later.
  void wireRouteHandler(PushRouteHandler handler) {
    _routeHandler = handler;
    _wireTapHandlers();
  }

  void _wireTapHandlers() {
    final messaging = _messaging;
    if (!_available || messaging == null || _routeHandler == null) return;
    if (!_tapHandlersWired) {
      _tapHandlersWired = true;
      FirebaseMessaging.onMessageOpenedApp.listen(_handleRemoteMessage);
    }
    if (!_initialMessageChecked) {
      _initialMessageChecked = true;
      unawaited(messaging.getInitialMessage().then((message) {
        if (message != null) _handleRemoteMessage(message);
      }).catchError((Object e) {
        Log.breadcrumb('push: initial notification route failed',
            category: 'push', data: {'error': '$e'});
      }));
    }
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
            // Expandable: show the full message when the notification is
            // pulled down, instead of a single truncated line.
            styleInformation: BigTextStyleInformation(
              n.body ?? '',
              contentTitle: n.title,
            ),
          ),
          iOS: const DarwinNotificationDetails(),
        ),
        payload: jsonEncode({
          ...message.data,
          if (n.title != null) '_title': n.title,
          if (n.body != null) '_body': n.body,
        }),
      );
    } catch (e) {
      Log.breadcrumb('push: foreground display failed',
          category: 'push', data: {'error': '$e'});
    }
  }

  void _handleRemoteMessage(RemoteMessage message) {
    final route = routeForNotificationData(
      message.data,
      title: message.notification?.title,
      body: message.notification?.body,
    );
    if (route != null) _openRoute(route);
  }

  void _handleLocalPayload(String? payload) {
    if (payload == null || payload.isEmpty) return;
    try {
      final decoded = jsonDecode(payload);
      if (decoded is! Map) return;
      final data = decoded.map((k, v) => MapEntry(k.toString(), v));
      final route = routeForNotificationData(data);
      if (route != null) _openRoute(route);
    } catch (e) {
      Log.breadcrumb('push: local notification payload ignored',
          category: 'push', data: {'error': '$e'});
    }
  }

  void _openRoute(String route) {
    final handler = _routeHandler;
    if (handler == null) return;
    try {
      final result = handler(route);
      if (result is Future) {
        unawaited(result.catchError((Object e) {
          Log.breadcrumb('push: route handler failed',
              category: 'push', data: {'error': '$e', 'route': route});
        }));
      }
    } catch (e) {
      Log.breadcrumb('push: route handler failed',
          category: 'push', data: {'error': '$e', 'route': route});
    }
  }

  @visibleForTesting
  static String? routeForNotificationData(
    Map<String, dynamic> data, {
    String? title,
    String? body,
  }) {
    for (final key in const [
      'route',
      'path',
      'deep_link',
      'deeplink',
      'link',
      'url',
    ]) {
      final route = _internalRoute(data[key]);
      if (route != null) return route;
    }

    final hay = [
      title,
      body,
      for (final entry in data.entries) entry.key,
      for (final entry in data.entries) entry.value,
    ].whereType<Object>().join(' ').toLowerCase();

    bool has(List<String> words) => words.any(hay.contains);
    if (has([
      'message.outbound',
      'message_outbound',
      'message-new',
      'message_new',
      'chat_message',
      'support message',
      'new message',
      'agent replied',
      'agent message',
      'live chat',
      'chat',
      'crm',
    ])) {
      if (has(['reseller'])) return '/reseller/chat';
      return '/support/chat';
    }
    if (has(['ticket', 'support'])) return '/support';
    if (has(
        ['invoice', 'payment', 'billing', 'suspend', 'overdue', 'charge'])) {
      return '/billing';
    }
    if (has(['usage', 'quota', 'data', 'cap'])) return '/usage';
    return '/dashboard/notifications';
  }

  static String? _internalRoute(Object? value) {
    final raw = value?.toString().trim();
    if (raw == null || raw.isEmpty) return null;
    if (raw.startsWith('/')) return raw;
    final uri = Uri.tryParse(raw);
    if (uri == null) return null;
    if (uri.scheme.isNotEmpty && uri.path.startsWith('/')) return uri.path;
    return null;
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
