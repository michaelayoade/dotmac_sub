import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/core/api_exception.dart';
import 'package:dotmac_portal/src/features/billing/topup_screen.dart';
import 'package:dotmac_portal/src/models/payment_method.dart';
import 'package:dotmac_portal/src/models/topup.dart';
import 'package:dotmac_portal/src/providers/data_providers.dart';
import 'package:dotmac_portal/src/repositories/billing_repository.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

TopupPage _page({TopupActiveRequest? activeRequest}) => TopupPage(
      providerType: 'paystack',
      currency: 'NGN',
      minAmount: 1000,
      maxAmount: 500000,
      providers: [
        PaymentProviderOption(
          providerType: OnlinePaymentProvider.paystack,
          label: 'Pay with Paystack',
        ),
      ],
      depositAllowed: activeRequest == null,
      activeDepositRequest: activeRequest,
    );

class _BillingRepository extends BillingRepository {
  _BillingRepository(this.page) : super(Dio());

  TopupPage page;
  final previewAmounts = <num>[];
  final previewFingerprints = <String>[];
  bool failNextPreview = false;
  int initiateCalls = 0;
  String? canceledIntentId;

  @override
  Future<TopupPage> topupPage() async => page;

  @override
  Future<TopupPreview> previewTopup(num amount) async {
    previewAmounts.add(amount);
    if (failNextPreview) {
      failNextPreview = false;
      throw ApiException('Preview temporarily unavailable', statusCode: 503);
    }
    final fingerprint = previewFingerprints.isEmpty
        ? List.filled(64, 'a').join()
        : previewFingerprints.removeAt(0);
    return TopupPreview.fromJson({
      'account_id': 'account-1',
      'currency': 'NGN',
      'requested_deposit': amount,
      'preview_fingerprint': fingerprint,
    });
  }

  @override
  Future<TopupInitiation> initiateTopup(
    num amount, {
    required String previewFingerprint,
    String? provider,
    String? paymentMethodId,
    String? idempotencyKey,
  }) async {
    initiateCalls++;
    throw StateError('Checkout should not start for a changed preview');
  }

  @override
  Future<void> cancelTopupIntent(String intentId) async {
    canceledIntentId = intentId;
    page = _page();
  }
}

Widget _app(_BillingRepository repository, {bool withRouter = false}) =>
    ProviderScope(
      overrides: [
        billingRepositoryProvider.overrideWithValue(repository),
        paymentMethodsProvider.overrideWith((_) async => const <SavedCard>[]),
      ],
      child: withRouter
          ? MaterialApp.router(
              routerConfig: GoRouter(
                initialLocation: '/topup',
                routes: [
                  GoRoute(
                    path: '/topup',
                    builder: (_, __) => const TopUpScreen(),
                  ),
                ],
              ),
            )
          : const MaterialApp(home: TopUpScreen()),
    );

void main() {
  testWidgets('pending deposit shows owner status without requesting a preview',
      (tester) async {
    final active = TopupActiveRequest.fromJson({
      'intent_id': 'intent-1',
      'phase': 'under_review',
      'next_action': 'wait_for_review',
      'provider_type': 'direct_bank_transfer',
      'reference': 'TRF-PENDING',
      'amount': '20000.00',
      'currency': 'NGN',
      'created_at': '2026-09-22T10:00:00Z',
      'observed_at': '2026-09-22T11:00:00Z',
      'message': 'Your transfer receipt is under review.',
      'can_cancel': false,
    });
    final repository = _BillingRepository(_page(activeRequest: active));

    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();

    expect(find.text('Your transfer receipt is under review.'), findsOneWidget);
    expect(find.text('Reference: TRF-PENDING'), findsOneWidget);
    expect(find.text('Upload receipt'), findsNothing);
    expect(find.text('Cancel'), findsNothing);
    expect(find.text('Pay with'), findsNothing);
    expect(find.byType(TextField), findsNothing);
    expect(repository.previewAmounts, isEmpty);

    repository.page = _page();
    await tester.tap(find.text('Refresh status'));
    await tester.pumpAndSettle();
    expect(find.byType(TextField), findsOneWidget);
  });

  testWidgets('pending direct bank transfer exposes upload and cancel actions',
      (tester) async {
    final active = TopupActiveRequest.fromJson({
      'intent_id': 'intent-2',
      'phase': 'awaiting_receipt',
      'next_action': 'upload_receipt',
      'provider_type': 'direct_bank_transfer',
      'reference': 'TRF-CANCEL',
      'amount': '15000.00',
      'currency': 'NGN',
      'created_at': '2026-09-24T10:00:00Z',
      'observed_at': '2026-09-24T10:05:00Z',
      'message': 'Upload your receipt or cancel this transfer.',
      'can_cancel': true,
    });
    final repository = _BillingRepository(_page(activeRequest: active));

    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();

    expect(find.text('Bank transfer pending'), findsOneWidget);
    expect(find.text('Upload receipt'), findsOneWidget);
    expect(find.text('Cancel'), findsOneWidget);
    expect(find.byType(TextField), findsNothing);

    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();
    expect(find.text('Cancel bank transfer?'), findsOneWidget);

    await tester.tap(find.text('Cancel transfer'));
    await tester.pumpAndSettle();

    expect(repository.canceledIntentId, 'intent-2');
    expect(find.text('Bank transfer canceled.'), findsOneWidget);
    expect(find.byType(TextField), findsOneWidget);
  });

  testWidgets('preview error explains failure and can be retried',
      (tester) async {
    final repository = _BillingRepository(_page())..failNextPreview = true;
    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), '1000');
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pump();

    expect(find.text('Preview temporarily unavailable'), findsOneWidget);
    expect(find.text('Retry preview'), findsOneWidget);
    expect(tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
        isNull);

    await tester.tap(find.text('Retry preview'));
    await tester.pumpAndSettle();

    expect(find.text('Allocation preview'), findsOneWidget);
    expect(find.text('Preview temporarily unavailable'), findsNothing);
    expect(tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
        isNotNull);
    expect(repository.previewAmounts, [1000, 1000]);
  });

  testWidgets('rapid amount changes request only the settled amount',
      (tester) async {
    final repository = _BillingRepository(_page());
    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), '1000');
    await tester.enterText(find.byType(TextField), '10000');
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pump();

    expect(repository.previewAmounts, [10000]);
    expect(find.text('Allocation preview'), findsOneWidget);
  });

  testWidgets('changed allocation requires another review before checkout',
      (tester) async {
    final repository = _BillingRepository(_page())
      ..previewFingerprints.addAll([
        List.filled(64, 'a').join(),
        List.filled(64, 'b').join(),
      ]);
    await tester.pumpWidget(_app(repository, withRouter: true));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), '1000');
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pump();
    await tester.ensureVisible(find.byType(FilledButton));
    await tester.tap(find.byType(FilledButton));
    await tester.pumpAndSettle();

    expect(find.text('Allocation changed. Review the updated preview.'),
        findsOneWidget);
    expect(repository.previewAmounts, [1000, 1000]);
    expect(repository.initiateCalls, 0);
  });
}
