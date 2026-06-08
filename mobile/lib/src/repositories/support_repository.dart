import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/page.dart';
import '../models/ticket.dart';

/// Wraps the support endpoints (app/api/support.py, prefix /support).
class SupportRepository {
  SupportRepository(this.dio);

  final Dio dio;

  /// GET /support/tickets?status=&limit=&offset=
  Future<Page<Ticket>> tickets({
    String? status,
    String? subscriberId,
    int limit = 50,
    int offset = 0,
  }) async {
    final data =
        await guard(() => dio.get('/support/tickets', queryParameters: {
              if (status != null) 'status': status,
              if (subscriberId != null) 'subscriber_id': subscriberId,
              'limit': limit,
              'offset': offset,
            }));
    return Page.fromJson(data as Map<String, dynamic>, Ticket.fromJson);
  }

  /// GET /support/tickets/{id}
  Future<Ticket> ticket(String id) async {
    final data = await guard(() => dio.get('/support/tickets/$id'));
    return Ticket.fromJson(data as Map<String, dynamic>);
  }

  /// POST /support/tickets
  Future<Ticket> createTicket({
    required String title,
    String? description,
    String priority = 'normal',
    String? ticketType,
    String? subscriberId,
  }) async {
    final data = await guard(() => dio.post('/support/tickets', data: {
          'title': title,
          if (description != null) 'description': description,
          'priority': priority,
          if (ticketType != null) 'ticket_type': ticketType,
          if (subscriberId != null) 'subscriber_id': subscriberId,
          'channel': 'web',
        }));
    return Ticket.fromJson(data as Map<String, dynamic>);
  }

  /// GET /support/tickets/{id}/comments
  Future<Page<TicketComment>> comments(String ticketId,
      {int limit = 100, int offset = 0}) async {
    final data = await guard(
        () => dio.get('/support/tickets/$ticketId/comments', queryParameters: {
              'limit': limit,
              'offset': offset,
            }));
    return Page.fromJson(data as Map<String, dynamic>, TicketComment.fromJson);
  }

  /// POST /support/tickets/{id}/comments
  Future<TicketComment> addComment(String ticketId, String body) async {
    final data = await guard(() => dio.post(
          '/support/tickets/$ticketId/comments',
          data: {'body': body, 'is_internal': false},
        ));
    return TicketComment.fromJson(data as Map<String, dynamic>);
  }
}
