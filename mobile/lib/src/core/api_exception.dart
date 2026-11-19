import 'package:dio/dio.dart';

/// Normalised API error surfaced to the UI layer.
///
/// The shared backend error boundary returns top-level `code`, `message`, and
/// `request_id` fields. Legacy endpoints may still return `detail` as a string
/// or as a `{code, message}` object.
class ApiException implements Exception {
  ApiException(
    this.message, {
    this.statusCode,
    this.code,
    this.requestId,
    this.details,
  });

  final String message;
  final int? statusCode;
  final String? code;
  final String? requestId;
  final Map<String, dynamic>? details;

  bool get isUnauthorized => statusCode == 401;

  bool get isLoginProfileUnavailable => code == 'LOGIN_PROFILE_UNAVAILABLE';

  /// True when the backend signals the password must be reset before login
  /// can complete (HTTP 428, code PASSWORD_RESET_REQUIRED).
  bool get isPasswordResetRequired =>
      statusCode == 428 || code == 'PASSWORD_RESET_REQUIRED';

  factory ApiException.fromDio(DioException e) {
    final response = e.response;
    final status = response?.statusCode;
    final data = response?.data;

    if (data is Map) {
      final message = data['message'];
      if (message != null && message.toString().trim().isNotEmpty) {
        return ApiException(
          message.toString(),
          statusCode: status,
          code: data['code']?.toString(),
          requestId: data['request_id']?.toString(),
          details: data['details'] is Map
              ? Map<String, dynamic>.from(data['details'] as Map)
              : data['detail'] is Map
              ? Map<String, dynamic>.from(data['detail'] as Map)
              : null,
        );
      }
      final detail = data['detail'];
      if (detail is String) {
        return ApiException(
          detail,
          statusCode: status,
          code: data['code']?.toString(),
          requestId: data['request_id']?.toString(),
        );
      }
      if (detail is Map) {
        return ApiException(
          (detail['message'] ?? 'Request failed').toString(),
          statusCode: status,
          code: detail['code']?.toString(),
          requestId: data['request_id']?.toString(),
          details: Map<String, dynamic>.from(detail),
        );
      }
    }

    final fallback = switch (e.type) {
      DioExceptionType.connectionTimeout ||
      DioExceptionType.sendTimeout ||
      DioExceptionType.receiveTimeout => 'The server took too long to respond.',
      DioExceptionType.connectionError =>
        'Could not reach the server. Check your connection.',
      _ => 'Something went wrong (${status ?? 'network error'}).',
    };
    return ApiException(fallback, statusCode: status);
  }

  @override
  String toString() => message;
}
