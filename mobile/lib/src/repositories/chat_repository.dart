import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/chat.dart';

/// Live chat. Opens a session via the sub broker (which asserts the
/// authenticated subscriber/reseller identity to the CRM), then talks to the
/// CRM chat_widget channel directly with the opaque visitor token.
///
/// Foreground delivery is REST polling (no websocket dependency); background
/// delivery is an FCM push driven by the CRM message.outbound webhook.
class ChatRepository {
  ChatRepository(this._dio);

  /// The sub API client (carries the bearer/session auth).
  final Dio _dio;

  /// A separate client for the CRM, scoped to one session's visitor token.
  Dio? _crm;

  /// Open (or resume) a session. [endpoint] is the broker path:
  /// `/me/chat/session` (customer) or `/reseller/chat/session` (reseller).
  Future<ChatSession> openSession({String endpoint = '/me/chat/session'}) async {
    final data = await guard(() => _dio.post(endpoint, data: const {}));
    final session = ChatSession.fromJson(data as Map<String, dynamic>);
    _crm = Dio(BaseOptions(
      baseUrl: session.apiBase,
      connectTimeout: const Duration(seconds: 15),
      receiveTimeout: const Duration(seconds: 20),
      contentType: Headers.jsonContentType,
      headers: {'X-Visitor-Token': session.visitorToken},
      validateStatus: (s) => s != null && s < 500,
    ));
    return session;
  }

  Dio get _crmClient {
    final c = _crm;
    if (c == null) {
      throw StateError('openSession() must be called before using the CRM API');
    }
    return c;
  }

  Future<List<ChatMessage>> history(ChatSession s, {int limit = 50}) async {
    final data = await guard(() => _crmClient.get(
          '/session/${s.sessionId}/messages',
          queryParameters: {'limit': limit},
        ));
    final list = (data as Map<String, dynamic>)['messages'] as List? ?? const [];
    return list
        .map((m) => ChatMessage.fromHistory(m as Map<String, dynamic>))
        .toList();
  }

  Future<ChatMessage> send(ChatSession s, String body) async {
    final data = await guard(() => _crmClient.post(
          '/session/${s.sessionId}/message',
          data: {'body': body},
        ));
    return ChatMessage.fromSendResponse(data as Map<String, dynamic>);
  }

  Future<void> markRead(ChatSession s) async {
    await guard(() => _crmClient.post('/session/${s.sessionId}/read'));
  }
}
