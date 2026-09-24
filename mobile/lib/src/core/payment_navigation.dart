import '../config/env.dart';

/// What the hosted-checkout screen should do with a requested navigation.
enum PaymentNavigationDisposition {
  navigateInWebView,
  tryExternalApp,
  launchExternalApp,
  complete,
  cancel,
  block,
}

/// Typed result of classifying a URL requested by the payment provider.
class PaymentNavigationTarget {
  const PaymentNavigationTarget(
    this.disposition, {
    this.uri,
    this.reference,
  });

  final PaymentNavigationDisposition disposition;
  final Uri? uri;
  final String? reference;
}

const _webViewOwnedSchemes = <String>{
  'about',
  'blob',
  'data',
  'javascript',
};

const _blockedSchemes = <String>{
  'content',
  'file',
};

/// Classifies provider navigation without performing any platform side effect.
///
/// Paystack's HTTPS checkout and browser-based bank/3DS redirects remain in
/// the WebView. HTTP(S) navigations are first offered to an installed app via
/// universal/app links, then fall back to the WebView. Custom app schemes are
/// handed to the OS directly. Local-device schemes are never opened.
PaymentNavigationTarget resolvePaymentNavigation(
  String rawUrl, {
  required String expectedReference,
}) {
  final uri = Uri.tryParse(rawUrl);
  if (uri == null || uri.scheme.isEmpty) {
    return const PaymentNavigationTarget(
      PaymentNavigationDisposition.block,
    );
  }

  final scheme = uri.scheme.toLowerCase();
  if (scheme == Brand.paymentScheme.toLowerCase()) {
    if (uri.host == 'success') {
      return PaymentNavigationTarget(
        PaymentNavigationDisposition.complete,
        uri: uri,
        reference: uri.queryParameters['reference'] ?? expectedReference,
      );
    }
    if (uri.host == 'cancel') {
      return PaymentNavigationTarget(
        PaymentNavigationDisposition.cancel,
        uri: uri,
      );
    }
    return PaymentNavigationTarget(
      PaymentNavigationDisposition.block,
      uri: uri,
    );
  }

  if (scheme == 'https' &&
      uri.host.toLowerCase() == 'standard.paystack.co' &&
      uri.path == '/close') {
    return PaymentNavigationTarget(
      PaymentNavigationDisposition.cancel,
      uri: uri,
    );
  }

  final returnedReference = uri.queryParameters['reference'];
  if (scheme == 'https' &&
      returnedReference == expectedReference &&
      uri.path.endsWith('/verify')) {
    return PaymentNavigationTarget(
      PaymentNavigationDisposition.complete,
      uri: uri,
      reference: returnedReference,
    );
  }

  if (scheme == 'http' || scheme == 'https') {
    return PaymentNavigationTarget(
      PaymentNavigationDisposition.tryExternalApp,
      uri: uri,
    );
  }

  if (_webViewOwnedSchemes.contains(scheme)) {
    return PaymentNavigationTarget(
      PaymentNavigationDisposition.navigateInWebView,
      uri: uri,
    );
  }

  if (_blockedSchemes.contains(scheme)) {
    return PaymentNavigationTarget(
      PaymentNavigationDisposition.block,
      uri: uri,
    );
  }

  // intent:// requires Intent.parseUri on Android. All other non-web schemes
  // (including opay://) can be handed to the platform URL launcher.
  return PaymentNavigationTarget(
    PaymentNavigationDisposition.launchExternalApp,
    uri: uri,
  );
}
