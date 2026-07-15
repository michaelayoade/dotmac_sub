import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import 'api_exception.dart';

/// A human-readable mapping of a payment failure plus optional retry guidance.
///
/// The backend returns specific error codes (e.g. `invalid_amount`,
/// `insufficient_balance`, `card_declined`) under `detail.code` where it can;
/// we fall back on the status code and finally a generic message. Screens use
/// [message] for the text and [action] to decide whether to offer
/// "Top up account" / "Try another method" / "Retry".
enum PaymentErrorAction { none, topUpAccount, tryAnotherMethod, retry }

class PaymentError {
  const PaymentError(this.message, {this.action = PaymentErrorAction.none});

  final String message;
  final PaymentErrorAction action;

  static PaymentError from(Object e) {
    if (e is ApiException) {
      final code = e.code?.toLowerCase();
      final status = e.statusCode;
      switch (code) {
        case 'insufficient_balance':
        case 'insufficient_funds':
          return const PaymentError(
            'Not enough account credit for this payment.',
            action: PaymentErrorAction.topUpAccount,
          );
        case 'invalid_amount':
          return const PaymentError('Enter a valid amount and try again.');
        case 'card_declined':
        case 'payment_declined':
          return const PaymentError(
            'Your card was declined. Try another payment method.',
            action: PaymentErrorAction.tryAnotherMethod,
          );
        case 'card_expired':
          return const PaymentError(
            'That card has expired. Use a different card.',
            action: PaymentErrorAction.tryAnotherMethod,
          );
      }
      // No specific code — lean on the status / message.
      if (status == 402) {
        return PaymentError(
          e.message,
          action: PaymentErrorAction.tryAnotherMethod,
        );
      }
      if (status == null) {
        // Transport-level (timeout / no connection) — surfaced by ApiException
        // with a null status. Offer a retry.
        return PaymentError(e.message, action: PaymentErrorAction.retry);
      }
      return PaymentError(e.message);
    }
    // Non-API error (unexpected) — generic, retryable.
    return const PaymentError(
      'Something went wrong. Please try again.',
      action: PaymentErrorAction.retry,
    );
  }
}

/// Show a payment failure as a SnackBar with the right contextual action:
///   * insufficient balance → "Top up account" (routes to /topup),
///   * declined / 402 → "Try another method" (just dismisses; the user can
///     pick a different rail),
///   * network/timeout → "Retry" (re-runs [onRetry] when provided).
void showPaymentError(
  BuildContext context,
  Object error, {
  VoidCallback? onRetry,
}) {
  final pe = PaymentError.from(error);
  final messenger = ScaffoldMessenger.of(context);
  SnackBarAction? action;
  switch (pe.action) {
    case PaymentErrorAction.topUpAccount:
      action = SnackBarAction(
        label: 'Top up account',
        onPressed: () => context.push('/topup'),
      );
    case PaymentErrorAction.retry:
      if (onRetry != null) {
        action = SnackBarAction(label: 'Retry', onPressed: onRetry);
      }
    case PaymentErrorAction.tryAnotherMethod:
    case PaymentErrorAction.none:
      action = null;
  }
  messenger.showSnackBar(SnackBar(content: Text(pe.message), action: action));
}
