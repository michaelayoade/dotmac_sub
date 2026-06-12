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
}
