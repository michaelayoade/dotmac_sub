import 'dart:ffi';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/connectivity.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/core/offline/sync_service.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/execution/execution_controller.dart';
import 'package:dotmac_field/features/today/map_assets_repository.dart';
import 'package:drift/native.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqlite3/open.dart';

import 'helpers/fake_http.dart';

void main() {
  setUpAll(() {
    if (Platform.isLinux) {
      open.overrideFor(
        OperatingSystem.linux,
        () => DynamicLibrary.open('libsqlite3.so.0'),
      );
    }
  });

  late AppDatabase db;
  late FakeHttpAdapter adapter;
  late FakeConnectivity connectivity;
  late SyncService sync;
  late ProviderContainer container;

  setUp(() async {
    db = AppDatabase(NativeDatabase.memory());
    adapter = FakeHttpAdapter();
    final store = InMemoryTokenStore();
    await store.save(
      accessToken: fakeJwt(
        expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
      ),
      refreshToken: 'r',
      loginMode: LoginMode.staff,
    );
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'));
    dio.httpClientAdapter = adapter;
    final api = ApiClient(
      baseUrl: 'https://test.local',
      tokenStore: store,
      dio: dio,
    );
    connectivity = FakeConnectivity();
    sync = SyncService(
      db: db,
      api: api,
      connectivity: connectivity,
      delay: (_) async {},
    );
    container = ProviderContainer(
      overrides: [
        apiClientProvider.overrideWithValue(api),
        syncServiceProvider.overrideWithValue(sync),
      ],
    );
  });

  tearDown(() async {
    container.dispose();
    await sync.dispose();
    await db.close();
  });

  MapAssetsRepository repo() => container.read(mapAssetsRepositoryProvider);

  MapPlaceSearchRepository searchRepo() =>
      container.read(mapPlaceSearchRepositoryProvider);

  test('first map asset load fetches the API and warms the cache', () async {
    adapter.on('GET', '/api/v1/field/map-assets', (options) {
      expect(options.queryParameters['types'], 'olt');
      return (
        200,
        {
          'items': [
            {
              'id': 'olt-1',
              'type': 'olt',
              'title': 'OLT Alpha',
              'subtitle': 'host-1',
              'latitude': 9.1,
              'longitude': 7.4,
              'status': 'active',
            },
          ],
          'count': 1,
        },
      );
    });

    final assets = await repo().fetchAssets({'olt'});

    expect(assets.single.id, 'olt-1');
    expect((await db.select(db.cachedMapAssets).get()).single.assetId, 'olt-1');
  });

  test('map place search reads online street results', () async {
    adapter.on('GET', '/api/v1/field/map-assets/search', (options) {
      expect(options.queryParameters['q'], 'Fiber Street');
      return (
        200,
        {
          'items': [
            {
              'kind': 'job',
              'id': 'job-1',
              'title': 'Install at Fiber Street',
              'latitude': 6.5,
              'longitude': 3.4,
              'status': 'dispatched',
              'address_text': '12 Fiber Street, Lekki',
            },
          ],
          'count': 1,
        },
      );
    });

    final results = await searchRepo().search('Fiber Street');

    expect(results.single.kind, 'job');
    expect(results.single.addressText, '12 Fiber Street, Lekki');
  });

  test('map place search stays local when offline', () async {
    connectivity.online = false;

    final results = await searchRepo().search('Fiber Street');

    expect(results, isEmpty);
  });

  test('fresh cached map assets return without another API request', () async {
    var calls = 0;
    adapter.on('GET', '/api/v1/field/map-assets', (_) {
      calls++;
      return (
        200,
        {
          'items': [
            {
              'id': 'fdh-1',
              'type': 'fdh',
              'title': 'FDH One',
              'subtitle': 'FDH-001',
              'latitude': 9.2,
              'longitude': 7.5,
              'status': 'active',
            },
          ],
          'count': 1,
        },
      );
    });

    expect(await repo().fetchAssets({'fdh'}), hasLength(1));
    expect(calls, 1);

    final cached = await repo().fetchAssets({'fdh'});

    expect(cached.single.title, 'FDH One');
    expect(calls, 1);
  });

  test(
    'stale cached map assets refresh with an updated_since cursor',
    () async {
      final oldCursor = DateTime.now().toUtc().subtract(
        const Duration(minutes: 10),
      );
      await db
          .into(db.cachedMapAssets)
          .insert(
            CachedMapAssetsCompanion.insert(
              assetType: 'olt',
              assetId: 'olt-1',
              title: 'OLT Alpha',
              latitude: 9.1,
              longitude: 7.4,
              cachedAt: DateTime.now().toUtc(),
            ),
          );
      await db
          .into(db.cachedMapAssetSyncCursors)
          .insert(
            CachedMapAssetSyncCursorsCompanion.insert(
              assetType: 'olt',
              syncedAt: oldCursor,
            ),
          );

      adapter.on('GET', '/api/v1/field/map-assets', (options) {
        expect(options.queryParameters['types'], 'olt');
        final updatedSince = DateTime.parse(
          options.queryParameters['updated_since'] as String,
        );
        expect(
          updatedSince.difference(oldCursor).abs(),
          lessThan(const Duration(seconds: 1)),
        );
        return (
          200,
          {
            'items': [
              {
                'id': 'olt-2',
                'type': 'olt',
                'title': 'OLT Beta',
                'latitude': 9.3,
                'longitude': 7.6,
                'status': 'active',
                'updated_at': DateTime.now().toUtc().toIso8601String(),
              },
            ],
            'count': 1,
            'server_time': DateTime.now().toUtc().toIso8601String(),
          },
        );
      });

      final cached = await repo().fetchAssets({'olt'});
      expect(cached.single.id, 'olt-1');

      var rows = await db.select(db.cachedMapAssets).get();
      for (var i = 0; i < 20 && rows.length < 2; i++) {
        await Future<void>.delayed(const Duration(milliseconds: 25));
        rows = await db.select(db.cachedMapAssets).get();
      }
      expect(rows.map((row) => row.assetId), containsAll(['olt-1', 'olt-2']));
    },
  );

  test('incremental refresh removes deleted cached map assets', () async {
    final oldCursor = DateTime.now().toUtc().subtract(
      const Duration(minutes: 10),
    );
    await db
        .into(db.cachedMapAssets)
        .insert(
          CachedMapAssetsCompanion.insert(
            assetType: 'fdh',
            assetId: 'fdh-deleted',
            title: 'FDH Deleted',
            latitude: 9.1,
            longitude: 7.4,
            cachedAt: DateTime.now().toUtc(),
          ),
        );
    await db
        .into(db.cachedMapAssetSyncCursors)
        .insert(
          CachedMapAssetSyncCursorsCompanion.insert(
            assetType: 'fdh',
            syncedAt: oldCursor,
          ),
        );

    adapter.on('GET', '/api/v1/field/map-assets', (_) {
      return (
        200,
        {
          'items': [],
          'deleted': [
            {
              'type': 'fdh',
              'id': 'fdh-deleted',
              'deleted_at': DateTime.now().toUtc().toIso8601String(),
            },
          ],
          'count': 0,
          'server_time': DateTime.now().toUtc().toIso8601String(),
        },
      );
    });

    final cached = await repo().fetchAssets({'fdh'});
    expect(cached.single.id, 'fdh-deleted');

    var rows = await db.select(db.cachedMapAssets).get();
    for (var i = 0; i < 20 && rows.isNotEmpty; i++) {
      await Future<void>.delayed(const Duration(milliseconds: 25));
      rows = await db.select(db.cachedMapAssets).get();
    }
    expect(rows, isEmpty);
  });
}
