import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/page.dart';
import '../models/ticket.dart';

/// Wraps the self-scoped support endpoints (app/api/me.py, prefix /me/support).
///
/// These require only authentication — the server forces every ticket to the
/// caller's own subscriber and strips staff-internal notes — unlike the
/// staff-gated /support/* endpoints, which return 403 for a subscriber token.
class SupportRepository {
  SupportRepository(this.dio);

  final Dio dio;

  /// GET /me/support/tickets?status=&limit=&offset=
  Future<Page<Ticket>> tickets({
    String? status,
    int limit = 50,
    int offset = 0,
  }) async {
    final data =
        await guard(() => dio.get('/me/support/tickets', queryParameters: {
              if (status != null) 'status': status,
              'limit': limit,
              'offset': offset,
            }));
    return Page.fromJson(data as Map<String, dynamic>, Ticket.fromJson);
  }

  /// GET /me/support/tickets/{id}
  Future<Ticket> ticket(String id) async {
    final data = await guard(() => dio.get('/me/support/tickets/$id'));
    return Ticket.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/support/tickets — scoped to the caller; no subscriber id sent.
  Future<Ticket> createTicket({
    required String title,
    String? description,
    String priority = 'normal',
    String? ticketType,
  }) async {
    final data = await guard(() => dio.post('/me/support/tickets', data: {
          'title': title,
          if (description != null) 'description': description,
          'priority': priority,
          if (ticketType != null) 'ticket_type': ticketType,
        }));
    return Ticket.fromJson(data as Map<String, dynamic>);
  }

  /// GET /me/support/tickets/{id}/comments
  Future<Page<TicketComment>> comments(String ticketId,
      {int limit = 100, int offset = 0}) async {
    final data = await guard(() =>
        dio.get('/me/support/tickets/$ticketId/comments', queryParameters: {
          'limit': limit,
          'offset': offset,
        }));
    return Page.fromJson(data as Map<String, dynamic>, TicketComment.fromJson);
  }

  /// POST /me/support/tickets/{id}/comments
  Future<TicketComment> addComment(String ticketId, String body) async {
    final data = await guard(() => dio.post(
          '/me/support/tickets/$ticketId/comments',
          data: {'body': body},
        ));
    return TicketComment.fromJson(data as Map<String, dynamic>);
  }
}
