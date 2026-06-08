/// Runtime configuration.
///
/// Override the API base URL at build/run time, e.g.:
///   flutter run --dart-define=API_BASE_URL=https://selfcare.dotmac.io
///
/// Defaults to the Android emulator loopback alias (10.0.2.2 -> host machine
/// localhost). For the iOS simulator use http://localhost:8000, and for a
/// physical device use your machine's LAN IP.
class Env {
  const Env._();

  static const String apiBaseUrl = String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'http://10.0.2.2:8000',
  );

  /// All backend routers are mounted under this prefix in app/main.py.
  static const String apiPrefix = '/api/v1';

  static String get apiRoot => '$apiBaseUrl$apiPrefix';

  /// GlitchTip DSN (Sentry-protocol) — crash reporting is OFF when empty (the
  /// default). Matches the backend's GLITCHTIP_DSN. Supply at build time:
  /// `--dart-define=GLITCHTIP_DSN=http://key@observability-host:8000/1`.
  static const String glitchtipDsn =
      String.fromEnvironment('GLITCHTIP_DSN', defaultValue: '');

  /// Deployment environment tag reported with crashes (production, staging…).
  static const String glitchtipEnvironment = String.fromEnvironment(
      'GLITCHTIP_ENVIRONMENT',
      defaultValue: 'production');

  /// Resolve a possibly-relative URL from the API (e.g. an avatar served at
  /// `/static/avatars/...`) into an absolute one against [apiBaseUrl].
  static String resolveUrl(String pathOrUrl) {
    if (pathOrUrl.startsWith('http://') || pathOrUrl.startsWith('https://')) {
      return pathOrUrl;
    }
    return '$apiBaseUrl$pathOrUrl';
  }
}
