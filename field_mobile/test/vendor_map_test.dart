import 'dart:async';

import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/vendor/vendor_map_screen.dart';
import 'package:dotmac_field/features/jobs/job_models.dart';
import 'package:dotmac_field/features/jobs/job_detail_screen.dart'
    show uriLauncherProvider;
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter/material.dart';

import 'helpers/fake_http.dart';

Future<ApiClient> client(FakeHttpAdapter adapter) async {
  final store = InMemoryTokenStore();
  await store.save(
    accessToken: fakeJwt(
      expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
    ),
    refreshToken: 'r',
    loginMode: LoginMode.vendor,
  );
  return ApiClient(
    baseUrl: 'https://test.local',
    tokenStore: store,
    dio: Dio(BaseOptions(baseUrl: 'https://test.local'))
      ..httpClientAdapter = adapter,
  );
}

void main() {
  testWidgets('vendor navigates an explicit assigned job destination', (
    tester,
  ) async {
    Uri? launched;
    var reads = 0;
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          vendorJobMapProvider.overrideWith((ref) async {
            reads++;
            return const VendorMapData(
              destinations: [
                VendorJobDestination(
                  jobId: 'job-1',
                  destination: JobDestination(
                    destinationType: 'project_target',
                    destinationId: 'cabinet',
                    label: 'Assigned cabinet',
                    latitude: 6.6,
                    longitude: 3.35,
                  ),
                ),
              ],
            );
          }),
          uriLauncherProvider.overrideWithValue((uri) async {
            launched = uri;
            return true;
          }),
        ],
        child: const MaterialApp(home: VendorMapScreen(showTiles: false)),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('vendor-job-job-1-cabinet')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('vendor-job-navigate')));
    await tester.pumpAndSettle();
    expect(launched?.queryParameters['destination'], '6.6,3.35');
    expect(reads, 2);
  });

  for (final outcome in ['revoked', 'error', 'removed', 'coordinates']) {
    testWidgets('open destination clears during $outcome refresh', (
      tester,
    ) async {
      final refresh = Completer<VendorMapData>();
      var reads = 0;
      var launches = 0;
      final container = ProviderContainer(
        overrides: [
          vendorJobMapProvider.overrideWith((ref) async {
            if (reads++ == 0) {
              return destinationData();
            }
            return refresh.future;
          }),
          uriLauncherProvider.overrideWithValue((uri) async {
            launches++;
            return true;
          }),
        ],
      );
      addTearDown(container.dispose);
      await tester.pumpWidget(
        UncontrolledProviderScope(
          container: container,
          child: const MaterialApp(home: VendorMapScreen(showTiles: false)),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('vendor-job-job-1-cabinet')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('vendor-job-navigate')), findsOneWidget);
      container.invalidate(vendorJobMapProvider);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 1));
      expect(find.text('Assigned cabinet'), findsNothing);
      expect(find.byKey(const Key('vendor-job-navigate')), findsNothing);
      if (outcome == 'revoked' || outcome == 'error') {
        refresh.completeError(StateError(outcome));
      } else if (outcome == 'coordinates') {
        refresh.complete(destinationData(latitude: 7.0));
      } else {
        refresh.complete(const VendorMapData(destinations: []));
      }
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('vendor-job-navigate')), findsNothing);
      expect(
        find.text('Destination unavailable. Refresh assigned jobs.'),
        findsOneWidget,
      );
      expect(launches, 0);
    });
  }

  for (final outcome in ['revoked', 'removed', 'coordinates']) {
    testWidgets('Navigate rechecks server authorization before $outcome', (
      tester,
    ) async {
      var reads = 0;
      var launches = 0;
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            vendorJobMapProvider.overrideWith((ref) async {
              if (reads++ == 0) {
                return destinationData();
              }
              if (outcome == 'revoked') {
                throw StateError('Job not found');
              }
              if (outcome == 'coordinates') {
                return destinationData(latitude: 7.0);
              }
              return const VendorMapData(destinations: []);
            }),
            uriLauncherProvider.overrideWithValue((uri) async {
              launches++;
              return true;
            }),
          ],
          child: const MaterialApp(home: VendorMapScreen(showTiles: false)),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('vendor-job-job-1-cabinet')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('vendor-job-navigate')));
      await tester.pumpAndSettle();
      expect(reads, 2);
      expect(launches, 0);
      expect(find.byKey(const Key('vendor-job-navigate')), findsNothing);
    });
  }

  test(
    'vendor map requests current job destinations without global plant or GPS',
    () async {
      final adapter = FakeHttpAdapter();
      adapter.on(
        'GET',
        '/api/v1/field/jobs',
        (_) => (
          200,
          {
            'items': [
              {'id': 'job-1'},
            ],
          },
        ),
      );
      adapter.on(
        'GET',
        '/api/v1/field/jobs/job-1/destinations',
        (_) => (
          200,
          {
            'items': [
              {
                'destination_type': 'customer',
                'destination_id': 'customer-1',
                'label': 'Customer site',
                'latitude': 6.6,
                'longitude': 3.35,
              },
              {
                'destination_type': 'project_target',
                'destination_id': 'cabinet',
                'label': 'Assigned cabinet',
                'latitude': 6.7,
                'longitude': 3.4,
              },
            ],
          },
        ),
      );
      final container = ProviderContainer(
        overrides: [apiClientProvider.overrideWithValue(await client(adapter))],
      );
      addTearDown(container.dispose);
      final data = await container.read(vendorJobMapProvider.future);
      expect(data.destinations.map((item) => item.destination.destinationId), [
        'customer-1',
        'cabinet',
      ]);
      expect(adapter.requests.map((request) => request.path), [
        '/api/v1/field/jobs',
        '/api/v1/field/jobs/job-1/destinations',
      ]);
    },
  );

  test(
    'reassignment refusal aborts vendor map instead of showing stale destinations',
    () async {
      final adapter = FakeHttpAdapter();
      adapter.on(
        'GET',
        '/api/v1/field/jobs',
        (_) => (
          200,
          {
            'items': [
              {'id': 'job-1'},
            ],
          },
        ),
      );
      adapter.on(
        'GET',
        '/api/v1/field/jobs/job-1/destinations',
        (_) => (404, {'detail': 'Job not found'}),
      );
      final container = ProviderContainer(
        overrides: [apiClientProvider.overrideWithValue(await client(adapter))],
      );
      addTearDown(container.dispose);
      await expectLater(
        container.read(vendorJobMapProvider.future),
        throwsA(isA<DioException>()),
      );
    },
  );

  testWidgets(
    'no assigned jobs shows empty state without global plant lookup',
    (tester) async {
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            vendorJobMapProvider.overrideWith(
              (ref) async => const VendorMapData(destinations: []),
            ),
          ],
          child: const MaterialApp(home: VendorMapScreen(showTiles: false)),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('No assigned job locations'), findsOneWidget);
    },
  );
}

VendorMapData destinationData({double latitude = 6.6}) => VendorMapData(
  destinations: [
    VendorJobDestination(
      jobId: 'job-1',
      destination: JobDestination(
        destinationType: 'project_target',
        destinationId: 'cabinet',
        label: 'Assigned cabinet',
        latitude: latitude,
        longitude: 3.35,
      ),
    ),
  ],
);
