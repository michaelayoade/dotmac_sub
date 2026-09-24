import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';

/// Result of asking the operating system to open a payment app.
class PaymentAppLaunchResult {
  const PaymentAppLaunchResult({
    required this.launched,
    this.fallbackUrl,
  });

  final bool launched;
  final Uri? fallbackUrl;
}

/// Injectable boundary between hosted-checkout navigation and the OS.
abstract interface class PaymentAppLauncher {
  Future<PaymentAppLaunchResult> launch(
    Uri uri, {
    required bool nonBrowserOnly,
  });
}

class PlatformPaymentAppLauncher implements PaymentAppLauncher {
  const PlatformPaymentAppLauncher();

  static const _androidChannel =
      MethodChannel('io.dotmac.selfcare/payment_app');

  @override
  Future<PaymentAppLaunchResult> launch(
    Uri uri, {
    required bool nonBrowserOnly,
  }) async {
    try {
      if (uri.scheme.toLowerCase() == 'intent') {
        if (defaultTargetPlatform != TargetPlatform.android) {
          return const PaymentAppLaunchResult(launched: false);
        }
        final response = await _androidChannel
            .invokeMapMethod<String, Object?>('launchIntentUri', {
          'url': uri.toString(),
        });
        final fallbackValue = response?['fallbackUrl'];
        return PaymentAppLaunchResult(
          launched: response?['launched'] == true,
          fallbackUrl: fallbackValue is String
              ? _secureFallbackUri(fallbackValue)
              : null,
        );
      }

      final launched = await launchUrl(
        uri,
        mode: nonBrowserOnly
            ? LaunchMode.externalNonBrowserApplication
            : LaunchMode.externalApplication,
      );
      return PaymentAppLaunchResult(launched: launched);
    } on PlatformException {
      return const PaymentAppLaunchResult(launched: false);
    } on FormatException {
      return const PaymentAppLaunchResult(launched: false);
    }
  }

  static Uri? _secureFallbackUri(String rawUrl) {
    final uri = Uri.tryParse(rawUrl);
    if (uri == null || uri.scheme != 'https' || uri.host.isEmpty) return null;
    return uri;
  }
}
