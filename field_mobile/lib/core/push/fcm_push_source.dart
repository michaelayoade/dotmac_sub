import 'dart:async';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';

import 'push_source.dart';

/// Real FCM transport. Satisfies [PushSource]; covers Android + iOS (APNs relay).
///
/// Constructed via [tryCreate], which returns null when Firebase isn't configured
/// (no `google-services.json` / `GoogleService-Info.plist` yet) so the app falls
/// back to [NoopPushSource]. It deliberately calls `Firebase.initializeApp()`
/// with no options (resolved from the native config files) so it carries no
/// compile-time dependency on a generated `firebase_options.dart`.
class FcmPushSource implements PushSource {
  FcmPushSource._(this._fm) {
    FirebaseMessaging.onMessage.listen(_emit);
    FirebaseMessaging.onMessageOpenedApp.listen((m) => _emit(m, fromTap: true));
  }

  final FirebaseMessaging _fm;
  final _messages = StreamController<PushMessage>.broadcast();

  /// Initialize Firebase + FCM. Returns null (→ NoopPushSource) when Firebase is
  /// not yet configured, so the app always boots regardless of Firebase setup.
  static Future<FcmPushSource?> tryCreate() async {
    try {
      await Firebase.initializeApp();
    } catch (_) {
      // Firebase not configured (run `flutterfire configure`) — push stays off.
      return null;
    }
    final fm = FirebaseMessaging.instance;
    try {
      await fm.requestPermission(alert: true, badge: true, sound: true);
    } catch (_) {
      // Permission prompt failures shouldn't block startup.
    }
    final source = FcmPushSource._(fm);
    await source._captureInitialMessage();
    return source;
  }

  /// Surface a notification tap that cold-launched the app from terminated state.
  Future<void> _captureInitialMessage() async {
    try {
      final initial = await _fm.getInitialMessage();
      if (initial != null) _emit(initial, fromTap: true);
    } catch (_) {}
  }

  @override
  Future<String?> get token async {
    try {
      return await _fm.getToken();
    } catch (_) {
      return null;
    }
  }

  @override
  Stream<String> get tokenRefresh => _fm.onTokenRefresh;

  @override
  Stream<PushMessage> get messages => _messages.stream;

  void _emit(RemoteMessage m, {bool fromTap = false}) {
    _messages.add(
      PushMessage(
        title: m.notification?.title,
        body: m.notification?.body,
        data: m.data.map((k, v) => MapEntry(k, '$v')),
        fromTap: fromTap,
      ),
    );
  }
}
