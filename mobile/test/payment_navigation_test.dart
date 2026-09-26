import 'package:dotmac_portal/src/config/env.dart';
import 'package:dotmac_portal/src/core/payment_navigation.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  const reference = 'TOPUP-123';

  test('completes a brand callback with its returned reference', () {
    final target = resolvePaymentNavigation(
      '${Brand.paymentScheme}://success?reference=$reference',
      expectedReference: 'other',
    );

    expect(target.disposition, PaymentNavigationDisposition.complete);
    expect(target.reference, reference);
  });

  test('cancels a brand callback', () {
    final target = resolvePaymentNavigation(
      '${Brand.paymentScheme}://cancel',
      expectedReference: reference,
    );

    expect(target.disposition, PaymentNavigationDisposition.cancel);
  });

  test('handles Paystack close as a cancellation', () {
    final target = resolvePaymentNavigation(
      'https://standard.paystack.co/close',
      expectedReference: reference,
    );

    expect(target.disposition, PaymentNavigationDisposition.cancel);
  });

  test('completes the matching HTTPS verification callback', () {
    final target = resolvePaymentNavigation(
      'https://api.dotmac.ng/api/v1/me/topup/verify?reference=$reference',
      expectedReference: reference,
    );

    expect(target.disposition, PaymentNavigationDisposition.complete);
    expect(target.reference, reference);
  });

  test('does not complete an HTTPS callback for another transaction', () {
    final target = resolvePaymentNavigation(
      'https://api.dotmac.ng/api/v1/me/topup/verify?reference=OTHER',
      expectedReference: reference,
    );

    expect(target.disposition, PaymentNavigationDisposition.tryExternalApp);
  });

  test('hands opay custom links to the operating system', () {
    final target = resolvePaymentNavigation(
      'opay://pay/authorize?token=opaque',
      expectedReference: reference,
    );

    expect(
      target.disposition,
      PaymentNavigationDisposition.launchExternalApp,
    );
  });

  test('hands Android intent links to the native launcher', () {
    final target = resolvePaymentNavigation(
      'intent://pay/authorize#Intent;scheme=opay;package=team.opay.pay;end',
      expectedReference: reference,
    );

    expect(
      target.disposition,
      PaymentNavigationDisposition.launchExternalApp,
    );
  });

  test('hands other bank app schemes to the operating system', () {
    final target = resolvePaymentNavigation(
      'bank-app://authorize?token=opaque',
      expectedReference: reference,
    );

    expect(
      target.disposition,
      PaymentNavigationDisposition.launchExternalApp,
    );
  });

  test('offers HTTPS universal links to an installed app first', () {
    final target = resolvePaymentNavigation(
      'https://checkout.opayweb.com/authorize',
      expectedReference: reference,
    );

    expect(target.disposition, PaymentNavigationDisposition.tryExternalApp);
  });

  test('keeps WebView-owned document schemes in the WebView', () {
    final target = resolvePaymentNavigation(
      'about:blank',
      expectedReference: reference,
    );

    expect(
      target.disposition,
      PaymentNavigationDisposition.navigateInWebView,
    );
  });

  test('blocks local file access from provider content', () {
    final target = resolvePaymentNavigation(
      'file:///data/user/0/io.dotmac.selfcare/private',
      expectedReference: reference,
    );

    expect(target.disposition, PaymentNavigationDisposition.block);
  });
}
