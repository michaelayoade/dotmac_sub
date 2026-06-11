import 'dart:async';

import 'package:app_links/app_links.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../config/env.dart';
import '../providers/data_providers.dart';
import 'api_exception.dart';

/// Catches the payment gateway's `<scheme>://success|cancel` return when a
/// 3-D Secure / bank flow leaves the in-app WebView and the OS hands the link
/// back to the app.
///
/// The in-app WebView intercepts the same redirect *before* the OS sees it
/// (see [PaymentWebViewScreen]), so this only fires for the out-of-WebView
/// case — no double handling. Verification is idempotent server-side (and the
/// gateway webhook is the ultimate safety net), so a duplicate verify is safe.
class PaymentLinkHandler {
  PaymentLinkHandler(this._ref, this._messengerKey);

  final WidgetRef _ref;
  final GlobalKey<ScaffoldMessengerState> _messengerKey;
  final AppLinks _appLinks = AppLinks();
  StreamSubscription<Uri>? _sub;
  bool _verifying = false;

  void start() {
    // Cold start: the app was launched by the deep link.
    _appLinks.getInitialLink().then((uri) {
      if (uri != null) _handle(uri);
    });
    // Warm: link arrives while the app is already running.
    _sub = _appLinks.uriLinkStream.listen(_handle, onError: (_) {});
  }

  void dispose() {
    _sub?.cancel();
  }

  bool _isPaymentReturn(Uri uri) => uri.scheme == Brand.paymentScheme;

  Future<void> _handle(Uri uri) async {
    if (!_isPaymentReturn(uri)) return;
    final messenger = _messengerKey.currentState;

    if (uri.host == 'cancel') {
      messenger?.showSnackBar(
        const SnackBar(content: Text('Payment canceled')),
      );
      return;
    }
    if (uri.host != 'success') return;

    final reference = uri.queryParameters['reference'];
    if (reference == null || reference.isEmpty) return;
    if (_verifying) return; // ignore a duplicate emission of the same link
    _verifying = true;

    try {
      final provider = uri.queryParameters['provider'];
      final result = await _ref
          .read(billingRepositoryProvider)
          .verifyPayment(reference, provider: provider);

      // Refresh everything that reflects a new payment.
      _ref.invalidate(invoicesProvider);
      _ref.invalidate(paymentsProvider);
      _ref.invalidate(balanceProvider);
      _ref.invalidate(ledgerProvider);

      messenger?.showSnackBar(
        SnackBar(
          content: Text(
            result.succeeded
                ? 'Payment of ${result.amount} ${result.currency} received'
                : 'Payment recorded (${result.status})',
          ),
        ),
      );
    } on ApiException catch (e) {
      messenger?.showSnackBar(SnackBar(content: Text(e.message)));
    } catch (_) {
      // The webhook credits independently; don't alarm the user on a transient
      // verify failure from the deep-link path.
    } finally {
      _verifying = false;
    }
  }
}
