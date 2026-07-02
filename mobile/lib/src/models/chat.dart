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

/// Delivery state of one of OUR messages (agent messages are always [sent]).
enum MessageStatus { sending, sent, failed }

class ChatMessage {
  ChatMessage({
    required this.id,
    required this.body,
    required this.fromAgent,
    this.authorName,
    this.authorAvatar,
    this.createdAt,
    this.readAt,
    this.status = MessageStatus.sent,
  });

  final String id;
  final String body;

  /// Delivery state — drives the "sending…/failed" indicator on our bubbles.
  final MessageStatus status;

  /// True when the message came from a support agent (CRM "outbound"); false
  /// for the subscriber's own messages.
  final bool fromAgent;
  final String? authorName;

  /// Agent's avatar URL (CRM `author_avatar`), present on outbound messages
  /// when the agent has a profile photo. Null for the subscriber's messages.
  final String? authorAvatar;
  final DateTime? createdAt;

  /// When an agent read this (our own) message — drives the "Seen" receipt.
  /// Null until read; only meaningful for the subscriber's own messages.
  final DateTime? readAt;

  ChatMessage copyWith({
    String? id,
    MessageStatus? status,
    DateTime? createdAt,
  }) => ChatMessage(
    id: id ?? this.id,
    body: body,
    fromAgent: fromAgent,
    authorName: authorName,
    authorAvatar: authorAvatar,
    createdAt: createdAt ?? this.createdAt,
    readAt: readAt,
    status: status ?? this.status,
  );

  static DateTime? _parseDate(Object? v) =>
      v is String ? DateTime.tryParse(v) : null;

  static String? _str(Object? v) {
    final s = v?.toString();
    return (s == null || s.isEmpty) ? null : s;
  }

  /// From GET /session/{id}/messages (WidgetMessageRead).
  factory ChatMessage.fromHistory(Map<String, dynamic> j) => ChatMessage(
    id: (j['id'] ?? '').toString(),
    body: (j['body'] ?? '').toString(),
    fromAgent: j['direction'] == 'outbound',
    authorName: _str(j['author_name']),
    authorAvatar: _str(j['author_avatar']),
    createdAt: _parseDate(j['created_at']),
    readAt: _parseDate(j['read_at']),
  );

  /// From POST /session/{id}/message (WidgetMessageResponse) — our own message.
  factory ChatMessage.fromSendResponse(Map<String, dynamic> j) => ChatMessage(
    id: (j['message_id'] ?? '').toString(),
    body: (j['body'] ?? '').toString(),
    fromAgent: false,
    createdAt: _parseDate(j['created_at']),
  );

  /// From a `message_new` WebSocket event (broadcast_to_widget_visitor).
  factory ChatMessage.fromSocket(Map<String, dynamic> j) => ChatMessage(
    id: (j['message_id'] ?? j['id'] ?? '').toString(),
    body: (j['body'] ?? '').toString(),
    fromAgent: j['direction'] == 'outbound',
    authorName: _str(j['author_name']),
    authorAvatar: _str(j['author_avatar']),
    createdAt: _parseDate(j['created_at']),
  );
}
