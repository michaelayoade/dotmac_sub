import 'package:dotmac_portal/src/features/billing/payment_webview_screen.dart';
import 'package:dotmac_portal/src/router/app_router.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('/pay redirects to Billing when opened without checkout context', () {
    expect(paymentRouteRedirect(null), '/billing');
    expect(paymentRouteRedirect('not checkout arguments'), '/billing');
  });

  test('/pay accepts an active checkout context', () {
    final args = CheckoutArgs(
      providerType: 'paystack',
      reference: 'PAY-123',
      amount: 1000,
      currency: 'NGN',
      metadata: const {},
      checkoutUrl: 'https://checkout.example.test/pay',
    );

    expect(paymentRouteRedirect(args), isNull);
  });
}
