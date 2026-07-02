import 'package:flutter/foundation.dart';
import 'package:sentry/sentry.dart';

/// Lightweight logging/breadcrumb layer.
///
/// Breadcrumbs are attached to GlitchTip (via the Sentry protocol) so a later
/// crash carries the trail that led to it — auth events, API calls, navigation.
/// They are a no-op when crash reporting is disabled (no DSN). In debug builds
/// everything also prints to the console.
///
/// Never pass secrets here (passwords, tokens, auth headers).
class Log {
  const Log._();

  static void breadcrumb(
    String message, {
    String category = 'app',
    SentryLevel level = SentryLevel.info,
    Map<String, dynamic>? data,
  }) {
    if (kDebugMode) {
      debugPrint('[$category] $message${data != null ? ' $data' : ''}');
    }
    Sentry.addBreadcrumb(
      Breadcrumb(
        message: message,
        category: category,
        level: level,
        data: data,
      ),
    );
  }

  /// Report a handled error (also leaves a breadcrumb). Uncaught errors are
  /// captured automatically by the handlers in main.dart.
  static void error(
    String message, {
    Object? error,
    StackTrace? stackTrace,
    String category = 'app',
  }) {
    if (kDebugMode) debugPrint('[$category:error] $message — $error');
    Sentry.addBreadcrumb(
      Breadcrumb(
        message: message,
        category: category,
        level: SentryLevel.error,
      ),
    );
  }
}
