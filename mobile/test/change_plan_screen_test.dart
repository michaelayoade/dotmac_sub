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
  _CatalogRepository() : super(Dio());

  String? quotedAddressId;

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
    return PlanChangeQuote(
      hasProration: false,
      previewFingerprint: List.filled(64, 'q').join(),
      previewEffectiveAt: DateTime.utc(2026, 9, 23),
    );
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
  testWidgets('selecting a service address updates quote target',
      (tester) async {
    final repository = _CatalogRepository();
    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();

    await tester.tap(find.byType(DropdownButtonFormField<String>));
    await tester.pumpAndSettle();
    await tester.tap(find.text('New address').last);
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);

    await tester.tap(find.text('Home 50'));
    await tester.pumpAndSettle();

    expect(repository.quotedAddressId, 'addr-new');
    expect(find.text('Switch to Home 50'), findsOneWidget);
  });
}
