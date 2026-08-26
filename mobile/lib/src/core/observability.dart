import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import 'package:sentry/sentry.dart';

/// Lightweight logging/breadcrumb layer.
///
/// Breadcrumbs are attached to GlitchTip (via the Sentry protocol) so a later
/// crash carries the trail that led to it — auth events, API calls, navigation.
/// They are a no-op when crash reporting is disabled (no DSN). In debug builds
/// everything also prints to the console.
///
/// Callers should not pass secrets here — but "should not" is not a control,
/// and the leaks that actually happened were incidental rather than careless:
/// interpolating a `DioException` into a breadcrumb drags in the full request
/// URL *and* the response body, and a path built with an inline query string
/// carries whatever was in it. So every message and every data value goes
/// through [redact] on the way out, and errors should be summarised with
/// [describeError] rather than interpolated. Redaction is the backstop, not
/// permission to be sloppy.
class Log {
  const Log._();

  /// Test seam: receives every breadcrumb *after* redaction, so a test can
  /// assert on exactly what would leave the device. Never set in production.
  @visibleForTesting
  static void Function(
      String message, String category, Map<String, dynamic>? data)? sink;

  static void breadcrumb(String message,
      {String category = 'app',
      SentryLevel level = SentryLevel.info,
      Map<String, dynamic>? data}) {
    final safeMessage = redact(message);
    final safeData = data == null
        ? null
        : {for (final e in data.entries) e.key: _redactValue(e.value)};
    if (kDebugMode) {
      debugPrint(
          '[$category] $safeMessage${safeData != null ? ' $safeData' : ''}');
    }
    sink?.call(safeMessage, category, safeData);
    Sentry.addBreadcrumb(Breadcrumb(
        message: safeMessage,
        category: category,
        level: level,
        data: safeData));
  }

  /// Report a handled error (also leaves a breadcrumb). Uncaught errors are
  /// captured automatically by the handlers in main.dart.
  static void error(String message,
      {Object? error, StackTrace? stackTrace, String category = 'app'}) {
    final safeMessage = redact(message);
    final safeError = describeError(error);
    if (kDebugMode) debugPrint('[$category:error] $safeMessage — $safeError');
    sink?.call(safeMessage, category, {'error': safeError});
    Sentry.addBreadcrumb(Breadcrumb(
        message: safeMessage, category: category, level: SentryLevel.error));
  }

  /// Summarise a thrown object for a breadcrumb without carrying its payload.
  ///
  /// A `DioException.toString()` is the specific hazard: it prints the request
  /// URI (query string and all) and, for a bad response, the entire response
  /// body. Reduce it to the two facts that are actually diagnostic — the
  /// failure kind and the status code — and drop everything else.
  static String describeError(Object? error) {
    if (error == null) return 'none';
    if (error is DioException) {
      final status = error.response?.statusCode;
      return status == null
          ? 'DioException(${error.type.name})'
          : 'DioException(${error.type.name}, $status)';
    }
    final text = redact(error.toString());
    return text.length <= 200 ? text : '${text.substring(0, 200)}…';
  }

  /// Strip the credential shapes that keep finding their way into log lines.
  ///
  /// Deliberately shape-based rather than name-based where it can be: a JWT is
  /// recognisable on sight, and recognising it catches the cases nobody thought
  /// to name.
  static String redact(String input) {
    var out = input;
    for (final rule in _rules) {
      out = out.replaceAllMapped(rule.pattern, rule.replace);
    }
    return out;
  }

  static Object? _redactValue(Object? value) =>
      value is String ? redact(value) : value;

  static final List<_RedactionRule> _rules = [
    // `Authorization: Bearer …` / a bare `Bearer …`, in any casing.
    _RedactionRule(
        RegExp(r'(authorization\s*[:=]\s*)\S+', caseSensitive: false),
        (m) => '${m[1]}[redacted]'),
    _RedactionRule(RegExp(r'\bBearer\s+\S+', caseSensitive: false),
        (_) => 'Bearer [redacted]'),
    // Anything JWT-shaped, wherever it appears (a URL, a body, a toString()).
    _RedactionRule(
        RegExp(r'\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b'),
        (_) => '[redacted-jwt]'),
    // Query-string and JSON secrets by name: `?token=…`, `"password":"…"`.
    _RedactionRule(
        RegExp(
            r'''(["']?(?:access_token|refresh_token|id_token|mfa_token|token|password|new_password|current_password|secret|api_key|apikey|code)["']?\s*[=:]\s*)(["']?)([^&\s,;}"']+)''',
            caseSensitive: false),
        (m) => '${m[1]}${m[2]}[redacted]')
  ];
}

class _RedactionRule {
  _RedactionRule(this.pattern, this.replace);

  final RegExp pattern;
  final String Function(Match match) replace;
}
