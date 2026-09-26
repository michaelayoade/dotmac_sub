import 'package:flutter_riverpod/flutter_riverpod.dart';

typedef PaymentReturnListener = bool Function(Uri uri);

/// Delivers an app-link payment return to the checkout screen that opened it.
///
/// The checkout screen owns the final verification because it knows whether
/// this is an invoice, top-up, quote deposit, or another payment flow.
class PaymentReturnCoordinator {
  PaymentReturnListener? _listener;
  Object? _listenerToken;

  Object attach(PaymentReturnListener listener) {
    final token = Object();
    _listenerToken = token;
    _listener = listener;
    return token;
  }

  void detach(Object token) {
    if (!identical(token, _listenerToken)) return;
    _listenerToken = null;
    _listener = null;
  }

  bool dispatch(Uri uri) => _listener?.call(uri) ?? false;
}

final paymentReturnCoordinatorProvider =
    Provider<PaymentReturnCoordinator>((_) => PaymentReturnCoordinator());
