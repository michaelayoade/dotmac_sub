import 'package:flutter/material.dart';

/// Runtime configuration.
///
/// Override the API base URL at build/run time, e.g.:
///   flutter run --dart-define=API_BASE_URL=https://selfcare.dotmac.io
///
/// Defaults to the production self-care API. For local development, override
/// this with the Android emulator loopback alias, localhost, or your machine's
/// LAN IP depending on the target device.
class Env {
  const Env._();

  static const String apiBaseUrl = String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'https://selfcare.dotmac.io',
  );

  /// All backend routers are mounted under this prefix in app/main.py.
  static const String apiPrefix = '/api/v1';

  static String get apiRoot => '$apiBaseUrl$apiPrefix';

  /// GlitchTip DSN (Sentry-protocol) — crash reporting is OFF when empty.
  /// Use a dedicated mobile GlitchTip project over HTTPS, not the backend DSN.
  /// Supply at build time:
  /// `--dart-define=GLITCHTIP_DSN=https://key@observability-host/<project-id>`.
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

/// White-label brand config, supplied at build time from the shared
/// `brand.json` at the repo root:
///   flutter build apk --dart-define-from-file=../brand.json
///
/// Keys mirror the backend's brand.json so a single file drives web and mobile.
///
/// Note: native app identity (applicationId / bundle id / launcher icon /
/// launcher label) is NOT configured here. Each organization is its own
/// deployment — it provisions its own domain and its own native app identity as
/// part of that setup. `Brand.name` here is only the in-app display name (the
/// MaterialApp title / login heading), not the OS launcher label.
class Brand {
  const Brand._();

  static const String name = String.fromEnvironment('BRAND_MOBILE_APP_NAME',
      defaultValue: 'Dotmac Selfcare');

  static const String tagline = String.fromEnvironment(
    'BRAND_TAGLINE',
    defaultValue: 'Sign in to manage your service',
  );

  /// Support contact + legal name shown on the About screen. From the shared
  /// brand.json (BRAND_SUPPORT_EMAIL / BRAND_LEGAL_NAME).
  static const String supportEmail =
      String.fromEnvironment('BRAND_SUPPORT_EMAIL', defaultValue: '');

  static const String legalName =
      String.fromEnvironment('BRAND_LEGAL_NAME', defaultValue: '');

  /// App version label for the About screen (set per release build).
  static const String version =
      String.fromEnvironment('APP_VERSION', defaultValue: '1.2.4');

  /// Hex brand colour (e.g. `#3b82f6`) used as the Material seed colour.
  static const String _primaryColorHex =
      String.fromEnvironment('BRAND_PRIMARY_COLOR', defaultValue: '#3b82f6');

  /// Custom URL scheme the payment WebView uses for success/cancel callbacks
  /// (e.g. `dotmacpay`). Kept unique per brand so two white-label apps on one
  /// device don't collide.
  static const String paymentScheme =
      String.fromEnvironment('BRAND_PAYMENT_SCHEME', defaultValue: 'dotmacpay');

  /// Parsed seed colour; falls back to a blue if the hex is malformed.
  static Color get primaryColor => _parseHexColor(_primaryColorHex);

  static Color _parseHexColor(String hex) {
    var value = hex.trim();
    if (value.startsWith('#')) value = value.substring(1);
    if (value.length == 6) value = 'FF$value';
    final parsed = int.tryParse(value, radix: 16);
    return Color(parsed ?? 0xFF3B82F6);
  }
}
