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
        subscriptionsProvider
            .overrideWith((_) async => model.Page<Subscription>(
                  items: [
                    service,
                    Subscription(
                      id: 'service-2',
                      accountId: 'customer-1',
                      offerId: 'airfiber-offer',
                      status: 'active',
                      billingMode: 'prepaid',
                      offerName: 'My Airfiber service',
                      offerAccessType: 'fixed_wireless',
                    ),
                  ],
                  count: 2,
                  limit: 2,
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
                  builder: (_) => const ServiceRequestSheet(
                    sourceSubscriptionId: 'service-1',
                  ),
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
    expect(find.text('Service to relocate'), findsNothing);
    expect(find.text('Relocating My fiber service'), findsOneWidget);
    expect(find.textContaining('Fiber to Fiber Relocation'), findsWidgets);
    expect(find.textContaining('Fiber to Airfiber Relocation'), findsWidgets);
    expect(find.textContaining('Airfiber to Fiber Relocation'), findsNothing);
    expect(find.textContaining('cable replacement'), findsNothing);
    expect(
      tester
          .widget<FilledButton>(find.widgetWithText(
            FilledButton,
            'Continue to location',
          ))
          .onPressed,
      isNull,
    );
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
        subscriptionsProvider
            .overrideWith((_) async => model.Page<Subscription>(
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
                  builder: (_) => const ServiceRequestSheet(
                    sourceSubscriptionId: 'service-1',
                  ),
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
    await tester.tap(find.text('Relocation type'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Fiber to Airfiber Relocation').last);
    await tester.pumpAndSettle();
    expect(
      tester
          .widget<FilledButton>(find.widgetWithText(
            FilledButton,
            'Continue to location',
          ))
          .onPressed,
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
    expect(selected?.subscriptionId, 'service-1');
  });

  testWidgets('suspended selected service cannot start relocation',
      (tester) async {
    await tester.pumpWidget(ProviderScope(
      overrides: [
        subscriptionsProvider
            .overrideWith((_) async => model.Page<Subscription>(
                  items: [
                    Subscription(
                      id: 'service-2',
                      accountId: 'customer-1',
                      offerId: 'fiber-plan',
                      status: 'suspended',
                      billingMode: 'prepaid',
                      offerName: 'Suspended fiber service',
                      offerAccessType: 'fiber',
                    ),
                  ],
                  count: 1,
                  limit: 1,
                  offset: 0,
                )),
      ],
      child: MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (context) => FilledButton(
              onPressed: () => showModalBottomSheet<ServiceRequestSelection>(
                context: context,
                isScrollControlled: true,
                builder: (_) => const ServiceRequestSheet(
                  sourceSubscriptionId: 'service-2',
                ),
              ),
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

    expect(find.text('Service to relocate'), findsNothing);
    expect(find.text('Relocating Suspended fiber service'), findsOneWidget);
    expect(find.textContaining('not active'), findsOneWidget);
    expect(find.text('Relocation type'), findsNothing);
    expect(
      tester
          .widget<FilledButton>(find.widgetWithText(
            FilledButton,
            'Continue to location',
          ))
          .onPressed,
      isNull,
    );
  });

  testWidgets('airfiber service only offers airfiber source moves',
      (tester) async {
    await tester.pumpWidget(ProviderScope(
      overrides: [
        subscriptionsProvider
            .overrideWith((_) async => model.Page<Subscription>(
                  items: [
                    Subscription(
                      id: 'service-3',
                      accountId: 'customer-1',
                      offerId: 'airfiber-plan',
                      status: 'active',
                      billingMode: 'prepaid',
                      offerName: 'My Airfiber service',
                      offerAccessType: 'fixed_wireless',
                    ),
                  ],
                  count: 1,
                  limit: 1,
                  offset: 0,
                )),
      ],
      child: MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (context) => FilledButton(
              onPressed: () => showModalBottomSheet<ServiceRequestSelection>(
                context: context,
                isScrollControlled: true,
                builder: (_) => const ServiceRequestSheet(
                  sourceSubscriptionId: 'service-3',
                ),
              ),
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

    expect(find.textContaining('Fiber to Fiber Relocation'), findsNothing);
    expect(find.textContaining('Fiber to Airfiber Relocation'), findsNothing);
    expect(find.textContaining('Airfiber to Fiber Relocation'), findsWidgets);
    expect(
      find.textContaining(
        'Airfiber to Airfiber Relocation (No cable replacement)',
      ),
      findsWidgets,
    );
    expect(
      find.textContaining(
        'Airfiber to Airfiber Relocation (With cable replacement)',
      ),
      findsWidgets,
    );
  });
}
