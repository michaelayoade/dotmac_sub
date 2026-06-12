import 'package:dio/dio.dart';

import '../core/http.dart';

/// Wraps the self-scoped push-token endpoints (app/api/me.py:
/// POST /me/push-tokens, DELETE /me/push-tokens/{token}).
class PushRepository {
  PushRepository(this.dio);

  final Dio dio;

  /// Register (or re-bind) this device's FCM token to the caller's account.
  Future<void> registerToken({
    required String token,
    required String platform,
  }) async {
    await guard(
      () => dio.post(
        '/me/push-tokens',
        data: {'token': token, 'platform': platform},
      ),
    );
  }

  /// De-register a device token (on logout / token rotation).
  Future<void> unregisterToken(String token) async {
    await guard(() => dio.delete('/me/push-tokens/$token'));
  }
}
