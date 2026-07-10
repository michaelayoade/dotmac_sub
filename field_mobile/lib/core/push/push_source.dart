import 'dart:async';

/// A push notification as the app consumes it.
class PushMessage {
  const PushMessage({
    this.title,
    this.body,
    this.data = const {},
    this.fromTap = false,
  });

  final String? title;
  final String? body;
  final Map<String, String> data;

  /// True when the user tapped the system notification (deep-link intent).
  final bool fromTap;
}

/// Push transport abstraction. The FCM implementation is only wired when
/// Firebase is configured (see fcm_push_source.dart); tests and headless
/// environments use [FakePushSource].
abstract class PushSource {
  /// Current registration token, null when push is unavailable.
  Future<String?> get token;

  /// Fired when the platform rotates the token — re-register with the API.
  Stream<String> get tokenRefresh;

  /// Incoming messages (foreground) and notification taps.
  Stream<PushMessage> get messages;
}

class NoopPushSource implements PushSource {
  const NoopPushSource();

  @override
  Future<String?> get token async => null;

  @override
  Stream<String> get tokenRefresh => const Stream.empty();

  @override
  Stream<PushMessage> get messages => const Stream.empty();
}

class FakePushSource implements PushSource {
  FakePushSource({String? initialToken}) : _token = initialToken;

  String? _token;
  final tokenController = StreamController<String>.broadcast();
  final messageController = StreamController<PushMessage>.broadcast();

  void rotateToken(String token) {
    _token = token;
    tokenController.add(token);
  }

  void emit(PushMessage message) => messageController.add(message);

  @override
  Future<String?> get token async => _token;

  @override
  Stream<String> get tokenRefresh => tokenController.stream;

  @override
  Stream<PushMessage> get messages => messageController.stream;
}

/// Deep-link resolution for backend push payloads.
/// Work-order assignment/comment pushes both open the job detail route.
String? routeForMessage(Map<String, String> data) {
  final workOrderId = data['work_order_id'];
  if (workOrderId == null || workOrderId.trim().isEmpty) return null;
  if (data['type'] == 'work_order_assigned' ||
      data['type'] == 'work_order_comment') {
    return '/jobs/$workOrderId';
  }
  return null;
}
