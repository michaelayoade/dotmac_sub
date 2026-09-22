import 'package:dotmac_portal/src/features/service/service_request_sheet.dart';
import 'package:dotmac_portal/src/models/page.dart' as model;
import 'package:dotmac_portal/src/models/plan_change.dart';
import 'package:dotmac_portal/src/models/service_request_option.dart';
import 'package:dotmac_portal/src/models/subscription.dart';
import 'package:dotmac_portal/src/providers/data_providers.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  testWidgets('customer selects an installation before continuing to the map',
      (tester) async {
    ServiceRequestSelection? selected;
    await tester.pumpWidget(ProviderScope(
      overrides: [
        subscriptionsProvider
            .overrideWith((_) async => model.Page<Subscription>(
                  items: const [],
                  count: 0,
                  limit: 0,
                  offset: 0,
                )),
      ],
      child: MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (context) => FilledButton(
              onPressed: () async {
                selected = await showModalBottomSheet<ServiceRequestSelection>(
                  context: context,
                  isScrollControlled: true,
                  builder: (_) => const ServiceRequestSheet(),
                );
              },
              child: const Text('Open'),
            ),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('Open'));
    await tester.pumpAndSettle();
    expect(
        tester
            .widget<FilledButton>(find.widgetWithText(
              FilledButton,
              'Continue to location',
            ))
            .onPressed,
        isNull);

    await tester.tap(find.text('Installation'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Installation type'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Airfiber Installation').last);
    await tester.pumpAndSettle();
    expect(find.textContaining('site check'), findsWidgets);

    await tester.ensureVisible(find.text('Continue to location'));
    await tester.tap(find.text('Continue to location'));
    await tester.pumpAndSettle();
    expect(selected?.option, ServiceRequestOption.airfiberInstallation);
    expect(selected?.subscriptionId, isNull);
  });

  testWidgets('relocation requires an active service and a matching move type',
      (tester) async {
    ServiceRequestSelection? selected;
    final service = Subscription(
      id: 'service-1',
      accountId: 'customer-1',
      offerId: 'offer-1',
      status: 'active',
      billingMode: 'prepaid',
      offerName: 'My fiber service',
      offerAccessType: 'fiber',
    );
    await tester.pumpWidget(ProviderScope(
      overrides: [
        subscriptionsProvider.overrideWith((_) async => model.Page<Subscription>(
              items: [service],
              count: 1,
              limit: 1,
              offset: 0,
            )),
      ],
      child: MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (context) => FilledButton(
              onPressed: () async {
                selected = await showModalBottomSheet<ServiceRequestSelection>(
                  context: context,
                  isScrollControlled: true,
                  builder: (_) => const ServiceRequestSheet(),
                );
              },
              child: const Text('Open'),
            ),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('Open'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Relocation'));
    await tester.pumpAndSettle();
    expect(find.textContaining('cable replacement'), findsWidgets);
    expect(
      tester.widget<FilledButton>(find.widgetWithText(
        FilledButton,
        'Continue to location',
      )).onPressed,
      isNull,
    );
    await tester.tap(find.text('Service to relocate'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('My fiber service').last);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Relocation type'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Fiber to Fiber Relocation').last);
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('Continue to location'));
    await tester.tap(find.text('Continue to location'));
    await tester.pumpAndSettle();

    expect(selected?.subscriptionId, 'service-1');
    expect(selected?.option, ServiceRequestOption.fiberToFiberRelocation);
  });

  testWidgets('technology change requires a destination plan', (tester) async {
    ServiceRequestSelection? selected;
    await tester.pumpWidget(ProviderScope(
      overrides: [
        subscriptionsProvider.overrideWith((_) async => model.Page<Subscription>(
              items: [
                Subscription(
                  id: 'service-1',
                  accountId: 'customer-1',
                  offerId: 'fiber-plan',
                  status: 'active',
                  billingMode: 'prepaid',
                  offerName: 'Current fiber plan',
                  offerAccessType: 'fiber',
                ),
              ],
              count: 1,
              limit: 1,
              offset: 0,
            )),
        relocationPlansProvider.overrideWith((_, __) async => [
              PlanOffer(
                id: 'airfiber-plan',
                name: 'Airfiber 50',
                amount: 20000,
                currency: 'NGN',
                periodLabel: '/month',
              ),
            ]),
      ],
      child: MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (context) => FilledButton(
              onPressed: () async {
                selected = await showModalBottomSheet<ServiceRequestSelection>(
                  context: context,
                  isScrollControlled: true,
                  builder: (_) => const ServiceRequestSheet(),
                );
              },
              child: const Text('Open'),
            ),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('Open'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Relocation'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Service to relocate'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Current fiber plan').last);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Relocation type'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Fiber to Airfiber Relocation').last);
    await tester.pumpAndSettle();
    expect(
      tester.widget<FilledButton>(find.widgetWithText(
        FilledButton,
        'Continue to location',
      )).onPressed,
      isNull,
    );
    await tester.ensureVisible(find.text('Plan at your new address'));
    await tester.tap(find.text('Plan at your new address'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Airfiber 50').last);
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('Continue to location'));
    await tester.tap(find.text('Continue to location'));
    await tester.pumpAndSettle();

    expect(selected?.destinationOfferId, 'airfiber-plan');
    expect(selected?.option, ServiceRequestOption.fiberToAirfiberRelocation);
  });
}
