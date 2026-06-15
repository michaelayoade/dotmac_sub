import 'package:dio/dio.dart';

import '../core/http.dart';
import '../core/token_storage.dart';
import '../models/auth.dart';
import '../models/session.dart';

/// Wraps the /auth/* endpoints (app/api/auth_flow.py).
class AuthRepository {
  AuthRepository({required this.dio, required this.storage});

  final Dio dio;
  final TokenStorage storage;

  /// POST /auth/login — may return an MFA challenge instead of tokens.
  Future<LoginResult> login({
    required String username,
    required String password,
    String? provider, // 'local' | 'radius'
  }) async {
    final data = await guard(() => dio.post(
          '/auth/login',
          data: {
            'username': username,
            'password': password,
            if (provider != null) 'provider': provider,
          },
          options: Options(extra: {'skipAuth': true}),
        ));
    final result = LoginResult.fromJson(data as Map<String, dynamic>);
    if (result.isAuthenticated) {
      await storage.save(
        accessToken: result.accessToken!,
        refreshToken: result.refreshToken,
      );
    }
    return result;
  }

  /// POST /auth/mfa/verify — exchange the MFA challenge token + TOTP code.
  Future<TokenPair> verifyMfa({
    required String mfaToken,
    required String code,
  }) async {
    final data = await guard(() => dio.post(
          '/auth/mfa/verify',
          data: {'mfa_token': mfaToken, 'code': code},
          options: Options(extra: {'skipAuth': true}),
        ));
    final pair = TokenPair.fromJson(data as Map<String, dynamic>);
    await storage.save(
      accessToken: pair.accessToken,
      refreshToken: pair.refreshToken,
    );
    return pair;
  }

  /// GET /auth/me
  Future<Me> me() async {
    final data = await guard(() => dio.get('/auth/me'));
    return Me.fromJson(data as Map<String, dynamic>);
  }

  /// PATCH /auth/me
  Future<Me> updateProfile(Map<String, dynamic> changes) async {
    final data = await guard(() => dio.patch('/auth/me', data: changes));
    return Me.fromJson(data as Map<String, dynamic>);
  }

  /// POST /auth/me/password
  Future<void> changePassword({
    required String currentPassword,
    required String newPassword,
  }) async {
    await guard(() => dio.post('/auth/me/password', data: {
          'current_password': currentPassword,
          'new_password': newPassword,
        }));
  }

  /// POST /auth/me/avatar (multipart field `file`) — returns the new avatar URL.
  Future<String> uploadAvatar({
    required List<int> bytes,
    required String filename,
    String? contentType,
  }) async {
    final ct = contentType ?? _guessImageContentType(filename);
    final form = FormData.fromMap({
      'file': MultipartFile.fromBytes(
        bytes,
        filename: filename,
        contentType: ct != null ? DioMediaType.parse(ct) : null,
      ),
    });
    final data = await guard(() => dio.post('/auth/me/avatar', data: form));
    return (data as Map)['avatar_url'].toString();
  }

  /// DELETE /auth/me/avatar — remove the current avatar.
  Future<void> deleteAvatar() async {
    await guard(() => dio.delete('/auth/me/avatar'));
  }

  /// GET /auth/me/sessions — the caller's active sessions.
  Future<List<AuthSessionInfo>> sessions() async {
    final data = await guard(() => dio.get('/auth/me/sessions'));
    final list = (data as Map)['sessions'] as List? ?? const [];
    return list
        .cast<Map<String, dynamic>>()
        .map(AuthSessionInfo.fromJson)
        .toList();
  }

  /// DELETE /auth/me/sessions/{id} — revoke one session.
  Future<void> revokeSession(String id) async {
    await guard(() => dio.delete('/auth/me/sessions/$id'));
  }

  /// DELETE /auth/me/sessions — revoke all sessions except the current one.
  Future<void> revokeOtherSessions() async {
    await guard(() => dio.delete('/auth/me/sessions'));
  }

  /// POST /auth/resend-verification-email — re-send the email-verification
  /// link to the signed-in customer. No body; returns whether a mail was sent.
  /// A 429 means rate-limited (3 / 15 min).
  Future<bool> resendVerificationEmail() async {
    final data = await guard(() => dio.post('/auth/resend-verification-email'));
    return (data as Map)['sent'] as bool? ?? false;
  }

  /// POST /auth/forgot-password
  Future<void> forgotPassword(String email) async {
    await guard(() => dio.post(
          '/auth/forgot-password',
          data: {'email': email},
          options: Options(extra: {'skipAuth': true}),
        ));
  }

  /// POST /auth/reset-password — complete a reset with the emailed token.
  Future<void> resetPassword({
    required String token,
    required String newPassword,
  }) async {
    await guard(() => dio.post(
          '/auth/reset-password',
          data: {'token': token, 'new_password': newPassword},
          options: Options(extra: {'skipAuth': true}),
        ));
  }

  static String? _guessImageContentType(String filename) {
    final ext = filename.toLowerCase().split('.').last;
    return switch (ext) {
      'jpg' || 'jpeg' => 'image/jpeg',
      'png' => 'image/png',
      'gif' => 'image/gif',
      'webp' => 'image/webp',
      'heic' => 'image/heic',
      _ => null,
    };
  }

  /// POST /auth/logout (best-effort) then clear local tokens.
  Future<void> logout() async {
    final refresh = await storage.readRefreshToken();
    try {
      await dio.post('/auth/logout', data: {'refresh_token': refresh});
    } catch (_) {
      // Logout should always succeed locally even if the network call fails.
    }
    await storage.clear();
  }
}
