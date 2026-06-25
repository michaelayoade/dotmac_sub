/// Models for the live-chat bridge. The sub broker returns an opaque visitor
/// token + the CRM endpoints the client then talks to directly.
class ChatSession {
  ChatSession({
    required this.sessionId,
    required this.visitorToken,
    required this.apiBase,
    required this.wsUrl,
    this.conversationId,
  });

  final String sessionId;
  final String visitorToken;
  final String apiBase;
  final String wsUrl;
  final String? conversationId;

  factory ChatSession.fromJson(Map<String, dynamic> j) => ChatSession(
        sessionId: j['session_id'] as String,
        visitorToken: j['visitor_token'] as String,
        apiBase: j['api_base'] as String,
        wsUrl: j['ws_url'] as String? ?? '',
        conversationId: j['conversation_id'] as String?,
      );
}

class ChatMessage {
  ChatMessage({
    required this.id,
    required this.body,
    required this.fromAgent,
    this.authorName,
    this.createdAt,
  });

  final String id;
  final String body;

  /// True when the message came from a support agent (CRM "outbound"); false
  /// for the subscriber's own messages.
  final bool fromAgent;
  final String? authorName;
  final DateTime? createdAt;

  static DateTime? _parseDate(Object? v) =>
      v is String ? DateTime.tryParse(v) : null;

  /// From GET /session/{id}/messages (WidgetMessageRead).
  factory ChatMessage.fromHistory(Map<String, dynamic> j) => ChatMessage(
        id: (j['id'] ?? '').toString(),
        body: (j['body'] ?? '').toString(),
        fromAgent: j['direction'] == 'outbound',
        authorName: j['author_name'] as String?,
        createdAt: _parseDate(j['created_at']),
      );

  /// From POST /session/{id}/message (WidgetMessageResponse) — our own message.
  factory ChatMessage.fromSendResponse(Map<String, dynamic> j) => ChatMessage(
        id: (j['message_id'] ?? '').toString(),
        body: (j['body'] ?? '').toString(),
        fromAgent: false,
        createdAt: _parseDate(j['created_at']),
      );
}
