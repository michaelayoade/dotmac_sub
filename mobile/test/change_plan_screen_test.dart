import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/features/service/change_plan_screen.dart';
import 'package:dotmac_portal/src/models/page.dart' as model;
import 'package:dotmac_portal/src/models/plan_change.dart';
import 'package:dotmac_portal/src/models/subscription.dart';
import 'package:dotmac_portal/src/providers/data_providers.dart';
import 'package:dotmac_portal/src/repositories/catalog_repository.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

class _CatalogRepository extends CatalogRepository {
  _CatalogRepository({this.quote}) : super(Dio());

  final PlanChangeQuote? quote;

  String? quotedAddressId;
  String? submittedAddressId;
  String? submittedFieldQuoteFingerprint;

  @override
  Future<PlanChangeOptions> planChangeOptions(String subscriptionId) async {
    return PlanChangeOptions(
      currentOffer: PlanOffer(
        id: 'offer-current',
        name: 'Home 20',
        amount: 10000,
        currency: 'NGN',
        periodLabel: '/month',
      ),
      availableOffers: [
        PlanOffer(
          id: 'offer-current',
          name: 'Home 20',
          amount: 10000,
          currency: 'NGN',
          periodLabel: '/month',
        ),
        PlanOffer(
          id: 'offer-next',
          name: 'Home 50',
          amount: 15000,
          currency: 'NGN',
          periodLabel: '/month',
        ),
      ],
      serviceAddresses: const [
        ServiceAddressOption(
          id: 'addr-current',
          label: 'Current address',
          hasCoordinates: true,
          isCurrent: true,
        ),
        ServiceAddressOption(
          id: 'addr-new',
          label: 'New address',
          hasCoordinates: true,
        ),
      ],
      currentServiceAddressId: 'addr-current',
    );
  }

  @override
  Future<PlanChangeQuote> planChangeQuote(
    String subscriptionId,
    String offerId, {
    String? targetServiceAddressId,
  }) async {
    quotedAddressId = targetServiceAddressId;
    return quote ??
        PlanChangeQuote(
          hasProration: false,
          previewFingerprint: List.filled(64, 'q').join(),
          previewEffectiveAt: DateTime.utc(2026, 9, 23),
        );
  }

  @override
  Future<PlanChangeResult> submitPlanChange(
    String subscriptionId, {
    required String offerId,
    required String previewFingerprint,
    required DateTime previewEffectiveAt,
    String? targetServiceAddressId,
    String? fieldQuoteFingerprint,
    String? notes,
  }) async {
    submittedAddressId = targetServiceAddressId;
    submittedFieldQuoteFingerprint = fieldQuoteFingerprint;
    return const PlanChangeResult(status: 'applied');
  }

  @override
  Future<model.Page<Subscription>> subscriptions({
    String? status,
    int limit = 50,
    int offset = 0,
  }) async {
    return model.Page(items: const [], count: 0, limit: limit, offset: offset);
  }
}

Subscription _service() => Subscription(
      id: 'sub-1',
      accountId: 'acct-1',
      offerId: 'offer-current',
      status: 'active',
      billingMode: 'prepaid',
      offerName: 'Home 20',
    );

Widget _app(_CatalogRepository repository) => ProviderScope(
      overrides: [
        catalogRepositoryProvider.overrideWithValue(repository),
      ],
      child: MaterialApp(home: ChangePlanScreen(service: _service())),
    );

void main() {
  testWidgets('plan changes do not expose or submit a service address',
      (tester) async {
    final repository = _CatalogRepository();
    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();

    expect(find.byType(DropdownButtonFormField<String>), findsNothing);
    expect(find.text('Service address'), findsNothing);
    expect(find.text('Home 20'), findsNothing);
    expect(tester.takeException(), isNull);

    await tester.tap(find.text('Home 50'));
    await tester.pumpAndSettle();

    expect(repository.quotedAddressId, isNull);
    expect(find.text('Switch to Home 50'), findsOneWidget);

    await tester.tap(find.text('Confirm'));
    await tester.pumpAndSettle();

    expect(repository.submittedAddressId, isNull);
    expect(repository.submittedFieldQuoteFingerprint, isNull);
    expect(tester.takeException(), isNull);
  });

  testWidgets('detailed confirmation is scrollable without overflow',
      (tester) async {
    tester.view.physicalSize = const Size(320, 568);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final repository = _CatalogRepository(
      quote: PlanChangeQuote(
        hasProration: true,
        chargeAmount: 12500,
        netAmount: 12500,
        prepaidFundingBefore: 3000,
        prepaidFundingAfter: -9500,
        postpaidReceivables: 2500,
        collectionBlockingBalance: 2500,
        shortfall: 9500,
        daysRemaining: 24,
        isUpgrade: true,
        previewFingerprint: List.filled(64, 'q').join(),
        previewEffectiveAt: DateTime.utc(2026, 9, 23),
        hasFinancialEffect: true,
        ledgerEntryType: 'plan_change_prorated_debit_adjustment',
        ledgerSource: 'subscription_plan_change_preview',
        ledgerAmount: 12500,
        accessConsequence: 'service_continues_after_verified_funding',
        deliveryMode: 'remote_reprovision',
      ),
    );
    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();

    await tester.ensureVisible(find.text('Home 50'));
    await tester.tap(find.text('Home 50'));
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    expect(find.text('Switch to Home 50'), findsOneWidget);

    await tester.scrollUntilVisible(
      find.text('Confirm'),
      200,
      scrollable: find.byType(Scrollable).last,
    );
    expect(find.text('Confirm'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
