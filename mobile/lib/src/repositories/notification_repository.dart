import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/notification.dart';
import '../models/page.dart';

/// Wraps the self-scoped notifications endpoint (app/api/me.py).
class NotificationRepository {
  NotificationRepository(this.dio);

  final Dio dio;

  /// GET /me/notifications — the subscriber's own notifications, newest first.
  Future<Page<AppNotification>> list({int limit = 50, int offset = 0}) async {
    final data =
        await guard(() => dio.get('/me/notifications', queryParameters: {
              'limit': limit,
              'offset': offset,
            }));
    return Page.fromJson(
        data as Map<String, dynamic>, AppNotification.fromJson);
  }

  /// Persist read state for selected notifications on the signed-in account.
  Future<int> markRead(Iterable<String> notificationIds) async {
    final ids = notificationIds.toSet().toList();
    if (ids.isEmpty) return 0;
    final data = await guard(() => dio.post(
          '/me/notifications/read',
          data: {
            'notification_ids': ids,
            'all_visible': false,
          },
        ));
    return (data as Map<String, dynamic>)['marked'] as int? ?? 0;
  }

  /// Persist read state for every notification visible to this account.
  Future<int> markAllRead() async {
    final data = await guard(() => dio.post(
          '/me/notifications/read',
          data: const {
            'notification_ids': <String>[],
            'all_visible': true,
          },
        ));
    return (data as Map<String, dynamic>)['marked'] as int? ?? 0;
  }
}
