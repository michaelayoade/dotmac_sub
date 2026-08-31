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

final RegExp _safeRouteSegment = RegExp(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$');

/// Deep-link resolution for backend push payloads.
/// Work-order assignment/comment pushes both open the job detail route.
String? routeForMessage(Map<String, String> data) {
  final version = data['contract_version']?.trim();
  if (version != null && version != 'PushIntentV1') return null;
  if (version == 'PushIntentV1') {
    for (final key in const [
      'intent_code',
      'subject_kind',
      'subject_id',
      'tenant_id',
      'principal_id',
      'issued_at',
    ]) {
      if (data[key]?.trim().isEmpty ?? true) return null;
    }
    if (DateTime.tryParse(data['issued_at']!)?.isUtc != true) return null;
  }
  final workOrderId = version == 'PushIntentV1'
      ? data['subject_id']
      : data['work_order_id'];
  if (workOrderId == null || !_safeRouteSegment.hasMatch(workOrderId.trim())) {
    return null;
  }
  final intentCode = version == 'PushIntentV1'
      ? data['intent_code']
      : data['type'];
  if (intentCode == 'work_order.assigned' ||
      intentCode == 'work_order.commented' ||
      intentCode == 'work_order_assigned' ||
      intentCode == 'work_order_comment') {
    return '/jobs/${workOrderId.trim()}';
  }
  return null;
}
