import 'dart:ffi';
import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/connectivity.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/core/offline/sync_service.dart';
import 'package:dotmac_field/core/secure/secure_field_store.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/execution/execution_controller.dart';
import 'package:dotmac_field/features/today/map_assets_repository.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqlite3/open.dart';

import 'helpers/fake_http.dart';
import 'helpers/secure_store.dart';

void main() {
  setUpAll(() {
    if (Platform.isLinux) {
      open.overrideFor(
        OperatingSystem.linux,
        () => DynamicLibrary.open('libsqlite3.so.0'),
      );
    }
  });

  late SecureFieldStore store;
  late AppDatabase db;
  late FakeHttpAdapter adapter;
  late FakeConnectivity connectivity;
  late SyncService sync;
  late ProviderContainer container;

  setUp(() async {
    store = await openTestStore();
    db = store.database;
    adapter = FakeHttpAdapter();
    final tokens = InMemoryTokenStore();
    await tokens.save(
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
      tokenStore: tokens,
      dio: dio,
    );
    connectivity = FakeConnectivity();
    sync = SyncService(
      db: db,
      api: api,
      connectivity: connectivity,
      evidence: store.evidence,
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
      expect(options.queryParameters['asset_type'], ['fdh_cabinet']);
      return (
        200,
        jsonDecode(
          File('test/fixtures/map_assets_page.json').readAsStringSync(),
        ),
      );
    });

    final assets = await repo().fetchAssets({'fdh_cabinet'});

    expect(assets.single.id, '00000000-0000-4000-8000-000000000001');
    expect(
      (await db.select(db.cachedMapAssets).get()).single.assetId,
      assets.single.id,
    );
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
          'limit': 1000,
          'offset': 0,
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
              'type': 'fdh_cabinet',
              'title': 'FDH One',
              'subtitle': 'FDH-001',
              'latitude': 9.2,
              'longitude': 7.5,
              'status': 'active',
            },
          ],
          'count': 1,
          'limit': 1000,
          'offset': 0,
        },
      );
    });

    expect(await repo().fetchAssets({'fdh_cabinet'}), hasLength(1));
    expect(calls, 1);

    final cached = await repo().fetchAssets({'fdh_cabinet'});

    expect(cached.single.title, 'FDH One');
    expect(calls, 1);
  });

  test('stale cached map assets refresh with a complete replacement', () async {
    final oldCursor = DateTime.now().toUtc().subtract(
      const Duration(minutes: 10),
    );
    await db
        .into(db.cachedMapAssets)
        .insert(
          CachedMapAssetsCompanion.insert(
            scopeKey: store.scopeKey,
            assetType: 'fdh_cabinet',
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
            scopeKey: store.scopeKey,
            assetType: 'fdh_cabinet',
            syncedAt: oldCursor,
          ),
        );

    adapter.on('GET', '/api/v1/field/map-assets', (options) {
      expect(options.queryParameters['asset_type'], ['fdh_cabinet']);
      expect(options.queryParameters.containsKey('updated_since'), isFalse);
      return (
        200,
        {
          'items': [
            {
              'id': 'olt-2',
              'type': 'fdh_cabinet',
              'title': 'OLT Beta',
              'latitude': 9.3,
              'longitude': 7.6,
              'status': 'active',
              'updated_at': DateTime.now().toUtc().toIso8601String(),
            },
          ],
          'count': 1,
          'limit': 1000,
          'offset': 0,
          'server_time': DateTime.now().toUtc().toIso8601String(),
        },
      );
    });

    final cached = await repo().fetchAssets({'fdh_cabinet'});
    expect(cached.single.id, 'olt-1');

    var rows = await db.select(db.cachedMapAssets).get();
    for (var i = 0; i < 20 && rows.single.assetId != 'olt-2'; i++) {
      await Future<void>.delayed(const Duration(milliseconds: 25));
      rows = await db.select(db.cachedMapAssets).get();
    }
    expect(rows.map((row) => row.assetId), ['olt-2']);
  });

  test('complete refresh removes assets absent from server snapshot', () async {
    final oldCursor = DateTime.now().toUtc().subtract(
      const Duration(minutes: 10),
    );
    await db
        .into(db.cachedMapAssets)
        .insert(
          CachedMapAssetsCompanion.insert(
            scopeKey: store.scopeKey,
            assetType: 'fdh_cabinet',
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
            scopeKey: store.scopeKey,
            assetType: 'fdh_cabinet',
            syncedAt: oldCursor,
          ),
        );

    adapter.on('GET', '/api/v1/field/map-assets', (_) {
      return (
        200,
        {
          'items': [],
          'count': 0,
          'limit': 1000,
          'offset': 0,
          'server_time': DateTime.now().toUtc().toIso8601String(),
        },
      );
    });

    final cached = await repo().fetchAssets({'fdh_cabinet'});
    expect(cached.single.id, 'fdh-deleted');

    var rows = await db.select(db.cachedMapAssets).get();
    for (var i = 0; i < 20 && rows.isNotEmpty; i++) {
      await Future<void>.delayed(const Duration(milliseconds: 25));
      rows = await db.select(db.cachedMapAssets).get();
    }
    expect(rows, isEmpty);
  });
  test(
    'pagination reads past a full page including invalid coordinates',
    () async {
      adapter.on('GET', '/api/v1/field/map-assets', (options) {
        final offset = options.queryParameters['offset'] as int;
        final count = offset == 0 ? 1000 : 1;
        return (
          200,
          {
            'items': List.generate(
              count,
              (i) => {
                'id': '${offset + i}',
                'type': 'fdh_cabinet',
                'title': 'Cabinet',
                'latitude': offset == 0 && i == 0 ? 91 : 9.1,
                'longitude': 7.4,
              },
            ),
            'count': count,
            'limit': 1000,
            'offset': offset,
          },
        );
      });
      final assets = await repo().fetchAssets({'fdh_cabinet'});
      expect(assets, hasLength(1000));
      expect(assets.last.id, '1000');
      expect(adapter.requests.map((r) => r.queryParameters['offset']), [
        0,
        1000,
      ]);
    },
  );

  test('a later page failure does not cache an incomplete snapshot', () async {
    adapter.on('GET', '/api/v1/field/map-assets', (options) {
      final offset = options.queryParameters['offset'] as int;
      if (offset > 0) return (503, {'detail': 'unavailable'});
      return (
        200,
        {
          'items': List.generate(
            1000,
            (i) => {
              'id': '$i',
              'type': 'fdh_cabinet',
              'title': 'Cabinet',
              'latitude': 9.1,
              'longitude': 7.4,
            },
          ),
          'count': 1000,
          'limit': 1000,
          'offset': 0,
        },
      );
    });
    await expectLater(
      repo().fetchAssets({'fdh_cabinet'}),
      throwsA(isA<DioException>()),
    );
    expect(await db.select(db.cachedMapAssets).get(), isEmpty);
    expect(await db.select(db.cachedMapAssetSyncCursors).get(), isEmpty);
  });
}
