/// Mirrors NotificationRead from app/schemas/notification.py — one message
/// addressed to the subscriber (email/sms/push/... record), shown in the
/// in-app inbox.
class AppNotification {
  AppNotification({
    required this.id,
    required this.channel,
    required this.status,
    this.isRead = false,
    this.category,
    this.eventType,
    this.subject,
    this.body,
    this.createdAt,
    this.sentAt,
  });

  final String id;
  final String channel; // email | sms | push | websocket | webhook
  final String status; // queued | sending | delivered | failed | canceled
  final bool isRead;
  final String? category;
  final String? eventType;
  final String? subject;
  final String? body;
  final DateTime? createdAt;
  final DateTime? sentAt;

  String get title {
    final s = subject?.trim();
    if (s != null && s.isNotEmpty) return s;
    final e = eventType?.trim();
    if (e != null && e.isNotEmpty) return e.replaceAll('_', ' ');
    return 'Notification';
  }

  factory AppNotification.fromJson(Map<String, dynamic> json) =>
      AppNotification(
        id: json['id'].toString(),
        channel: json['channel'] as String? ?? 'email',
        status: json['status'] as String? ?? 'queued',
        isRead: json['is_read'] as bool? ?? false,
        category: json['category'] as String?,
        eventType: json['event_type'] as String?,
        subject: json['subject'] as String?,
        body: json['body'] as String?,
        createdAt: _toDate(json['created_at']),
        sentAt: _toDate(json['sent_at']),
      );
}

DateTime? _toDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}
