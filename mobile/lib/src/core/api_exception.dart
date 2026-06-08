import 'package:dio/dio.dart';

/// Normalised API error surfaced to the UI layer.
///
/// The backend returns either `{"detail": "message"}` or, for auth flows,
/// `{"detail": {"code": "...", "message": "..."}}` (see schemas/auth_flow.py).
class ApiException implements Exception {
  ApiException(this.message, {this.statusCode, this.code});

  final String message;
  final int? statusCode;
  final String? code;

  bool get isUnauthorized => statusCode == 401;

  /// True when the backend signals the password must be reset before login
  /// can complete (HTTP 428, code PASSWORD_RESET_REQUIRED).
  bool get isPasswordResetRequired =>
      statusCode == 428 || code == 'PASSWORD_RESET_REQUIRED';

  factory ApiException.fromDio(DioException e) {
    final response = e.response;
    final status = response?.statusCode;
    final data = response?.data;

    if (data is Map) {
      final detail = data['detail'];
      if (detail is String) {
        return ApiException(detail, statusCode: status);
      }
      if (detail is Map) {
        return ApiException(
          (detail['message'] ?? 'Request failed').toString(),
          statusCode: status,
          code: detail['code']?.toString(),
        );
      }
    }

    final fallback = switch (e.type) {
      DioExceptionType.connectionTimeout ||
      DioExceptionType.sendTimeout ||
      DioExceptionType.receiveTimeout =>
        'The server took too long to respond.',
      DioExceptionType.connectionError =>
        'Could not reach the server. Check your connection.',
      _ => 'Something went wrong (${status ?? 'network error'}).',
    };
    return ApiException(fallback, statusCode: status);
  }

  @override
  String toString() => message;
}
