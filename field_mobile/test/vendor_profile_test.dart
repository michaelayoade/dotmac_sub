import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/jobs/jobs_providers.dart';
import 'package:dotmac_field/features/profile/profile_screen.dart';
import 'package:dotmac_field/features/profile/vendor_profile_provider.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fake_http.dart';

class _VendorAuthController extends AuthController {
  @override
  AuthState build() =>
      const Authenticated(LoginMode.vendor, vendorId: 'vendor-1');
}

const _meWire = <String, Object?>{
  'id': 'abbd13dc-c022-489d-b1d7-0ddfe3cf949a',
  'first_name': 'Miracle',
  'last_name': 'David',
  'display_name': 'Miracle D.',
  'email': 'miracle@example.test',
  'user_type': 'vendor',
  'roles': <String>['vendors'],
  'scopes': <String>[],
};

void main() {
  late FakeHttpAdapter adapter;
  late InMemoryTokenStore tokens;
  late ApiClient api;

  setUp(() async {
    adapter = FakeHttpAdapter();
    tokens = InMemoryTokenStore();
    await tokens.save(
      accessToken: fakeJwt(
        expiry: DateTime.now().toUtc().add(const Duration(hours: 1)),
      ),
      refreshToken: 'test-refresh',
      loginMode: LoginMode.vendor,
    );
    final dio = Dio()..httpClientAdapter = adapter;
    api = ApiClient(
      baseUrl: 'https://test.local',
      tokenStore: tokens,
      dio: dio,
    );
  });

  test(
    'canonical profile adapter returns typed identity and preserves vendor mode',
    () async {
      adapter.on('GET', '/api/v1/auth/me', (_) => (200, _meWire));
      final profile = await VendorProfileRepository(api).fetchMe();
      expect(profile.name, 'Miracle D.');
      expect(profile.email, 'miracle@example.test');
      expect(profile.vendorName, isNull);
      expect(profile.vendorRole, isNull);
      expect(await tokens.loginMode, LoginMode.vendor);
      expect(adapter.requests.map((request) => request.path), [
        '/api/v1/auth/me',
      ]);
    },
  );

  test('canonical first and last name are the display-name fallback', () async {
    adapter.on(
      'GET',
      '/api/v1/auth/me',
      (_) => (200, {..._meWire, 'display_name': ' '}),
    );
    expect(
      (await VendorProfileRepository(api).fetchMe()).name,
      'Miracle David',
    );
  });

  test(
    'invalid canonical identity fails instead of producing blank profile',
    () async {
      adapter.on(
        'GET',
        '/api/v1/auth/me',
        (_) => (200, {..._meWire, 'email': 42}),
      );
      await expectLater(
        VendorProfileRepository(api).fetchMe(),
        throwsFormatException,
      );
    },
  );

  Widget screen({
    String? queueFailure,
    bool Function()? recovered,
  }) => ProviderScope(
    overrides: [
      authControllerProvider.overrideWith(_VendorAuthController.new),
      apiClientProvider.overrideWithValue(api),
      meProvider.overrideWith(
        (ref) => throw StateError('Technician profile must not load'),
      ),
      pendingOutboxProvider.overrideWith(
        (ref) => queueFailure == 'actions' && recovered?.call() != true
            ? Stream<List<OutboxEntry>>.error(StateError('Queue unavailable'))
            : Stream.value([]),
      ),
      conflictOutboxProvider.overrideWith(
        (ref) => queueFailure == 'conflicts' && recovered?.call() != true
            ? Stream<List<OutboxEntry>>.error(StateError('Queue unavailable'))
            : Stream.value([]),
      ),
      pendingPhotosProvider.overrideWith(
        (ref) => queueFailure == 'photos' && recovered?.call() != true
            ? Stream<int>.error(StateError('Queue unavailable'))
            : Stream.value(0),
      ),
    ],
    child: const MaterialApp(home: ProfileScreen()),
  );

  testWidgets(
    'vendor screen renders canonical identity without fabricated organization',
    (tester) async {
      adapter.on('GET', '/api/v1/auth/me', (_) => (200, _meWire));
      await tester.pumpWidget(screen());
      await tester.pumpAndSettle();
      expect(find.text('Miracle D.'), findsOneWidget);
      expect(find.text('miracle@example.test'), findsOneWidget);
      expect(find.textContaining('open ·'), findsNothing);
      expect(find.text('vendors'), findsNothing);
      expect(adapter.requests.map((request) => request.path), [
        '/api/v1/auth/me',
      ]);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'vendor profile failure is visible and retry recovers without technician request',
    (tester) async {
      adapter.on(
        'GET',
        '/api/v1/auth/me',
        (_) => (503, {'detail': 'Unavailable'}),
      );
      await tester.pumpWidget(screen());
      await tester.pumpAndSettle();
      expect(find.text('Could not load your profile'), findsOneWidget);
      expect(find.text('Check your connection and try again.'), findsOneWidget);
      adapter.on('GET', '/api/v1/auth/me', (_) => (200, _meWire));
      await tester.tap(find.byKey(const Key('retry-vendor-profile')));
      await tester.pumpAndSettle();
      expect(find.text('Could not load your profile'), findsNothing);
      expect(find.text('Miracle D.'), findsOneWidget);
      expect(adapter.requests.map((request) => request.path), [
        '/api/v1/auth/me',
        '/api/v1/auth/me',
      ]);
      expect(await tokens.loginMode, LoginMode.vendor);
      expect(tester.takeException(), isNull);
    },
  );
  for (final queue in ['actions', 'conflicts', 'photos']) {
    testWidgets(
      '$queue queue error never renders false zero counts and retries',
      (tester) async {
        var recovered = false;
        adapter.on('GET', '/api/v1/auth/me', (_) => (200, _meWire));
        await tester.pumpWidget(
          screen(queueFailure: queue, recovered: () => recovered),
        );
        await tester.pumpAndSettle();
        expect(find.text('Sync status unavailable'), findsOneWidget);
        expect(find.byKey(const Key('sync-counts')), findsNothing);
        expect(find.byKey(const Key('sync-now')), findsNothing);
        expect(tester.takeException(), isNull);
        recovered = true;
        await tester.tap(find.byKey(const Key('retry-sync-status')));
        await tester.pumpAndSettle();
        expect(find.text('Sync status unavailable'), findsNothing);
        expect(find.text('0 queued actions · 0 queued photos'), findsOneWidget);
        expect(find.byKey(const Key('sync-now')), findsOneWidget);
        expect(tester.takeException(), isNull);
      },
    );
  }
}
