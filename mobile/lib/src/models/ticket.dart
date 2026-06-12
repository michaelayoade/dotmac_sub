/// Mirrors TicketRead and TicketCommentRead from app/schemas/support.py.
class Ticket {
  Ticket({
    required this.id,
    required this.title,
    required this.status,
    required this.priority,
    this.number,
    this.description,
    this.ticketType,
    this.channel,
    this.tags = const [],
    this.createdAt,
    this.updatedAt,
    this.resolvedAt,
    this.closedAt,
  });

  final String id;
  final String title;
  final String status;
  final String priority;
  final String? number;
  final String? description;
  final String? ticketType;
  final String? channel;
  final List<String> tags;
  final DateTime? createdAt;
  final DateTime? updatedAt;
  final DateTime? resolvedAt;
  final DateTime? closedAt;

  bool get isOpen =>
      closedAt == null && status != 'closed' && status != 'resolved';

  factory Ticket.fromJson(Map<String, dynamic> json) => Ticket(
        id: json['id'].toString(),
        title: json['title'] as String? ?? '(no title)',
        status: json['status'] as String? ?? 'open',
        priority: json['priority'] as String? ?? 'normal',
        number: json['number'] as String?,
        description: json['description'] as String?,
        ticketType: json['ticket_type'] as String?,
        channel: json['channel'] as String?,
        tags: (json['tags'] as List? ?? const [])
            .map((e) => e.toString())
            .toList(),
        createdAt: _toDate(json['created_at']),
        updatedAt: _toDate(json['updated_at']),
        resolvedAt: _toDate(json['resolved_at']),
        closedAt: _toDate(json['closed_at']),
      );
}

class TicketComment {
  TicketComment({
    required this.id,
    required this.ticketId,
    required this.body,
    this.isInternal = false,
    this.createdAt,
  });

  final String id;
  final String ticketId;
  final String body;
  final bool isInternal;
  final DateTime? createdAt;

  factory TicketComment.fromJson(Map<String, dynamic> json) => TicketComment(
        id: json['id'].toString(),
        ticketId: json['ticket_id'].toString(),
        body: json['body'] as String? ?? '',
        isInternal: json['is_internal'] as bool? ?? false,
        createdAt: _toDate(json['created_at']),
      );
}

DateTime? _toDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}
