/// Customer-scoped visit conversation returned by the native field-chat owner.
class FieldVisitMessage {
  const FieldVisitMessage({
    required this.id,
    required this.body,
    required this.fromCustomer,
    required this.createdAt,
    this.authorName,
  });

  final String id;
  final String body;
  final bool fromCustomer;
  final DateTime createdAt;
  final String? authorName;

  factory FieldVisitMessage.fromJson(Map<String, dynamic> json) =>
      FieldVisitMessage(
        id: json['id'] as String,
        body: json['body'] as String,
        fromCustomer: json['from_customer'] as bool,
        createdAt: DateTime.parse(json['created_at'] as String),
        authorName: json['author_name'] as String?,
      );
}

class FieldVisitChat {
  const FieldVisitChat({
    required this.available,
    required this.canSend,
    this.reason,
    this.technicianName,
    this.messages = const [],
  });

  final bool available;
  final bool canSend;
  final String? reason;
  final String? technicianName;
  final List<FieldVisitMessage> messages;

  factory FieldVisitChat.fromJson(Map<String, dynamic> json) => FieldVisitChat(
        available: json['available'] as bool? ?? false,
        canSend: json['can_send'] as bool? ?? false,
        reason: json['reason'] as String?,
        technicianName: json['technician_name'] as String?,
        messages: [
          for (final message in json['messages'] as List? ?? const [])
            FieldVisitMessage.fromJson(
                (message as Map).cast<String, dynamic>()),
        ],
      );
}
