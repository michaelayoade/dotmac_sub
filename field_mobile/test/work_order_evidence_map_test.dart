import 'dart:ffi' show DynamicLibrary;
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:dotmac_field/app/theme.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/connectivity.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/core/offline/sync_service.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/execution/execution_controller.dart';
import 'package:dotmac_field/features/jobs/work_order_evidence_map_models.dart';
import 'package:dotmac_field/features/jobs/work_order_evidence_map_repository.dart';
import 'package:dotmac_field/features/jobs/work_order_evidence_map_screen.dart';
import 'package:drift/native.dart';
import 'package:flutter/material.dart';
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
  late InMemoryTokenStore store;
  late SyncService sync;
  late ProviderContainer container;

  setUp(() async {
    db = AppDatabase(NativeDatabase.memory());
    adapter = FakeHttpAdapter();
    store = InMemoryTokenStore();
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
    sync = SyncService(
      db: db,
      api: api,
      connectivity: FakeConnectivity(),
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

  WorkOrderEvidenceMapRepository repo() =>
      container.read(workOrderEvidenceMapRepositoryProvider);

  test(
    'network read warms an exact work-order and report hash cache',
    () async {
      adapter.on('GET', '/api/v1/field/fiber/work-order-evidence-map', (
        options,
      ) {
        expect(options.queryParameters, {'work_order_id': 'WO-1'});
        return (200, _response('WO-1', reportChar: 'a'));
      });

      final snapshot = await repo().fetch('WO-1');
      final rows = await db.select(db.cachedWorkOrderEvidenceMaps).get();

      expect(snapshot.fromCache, isFalse);
      expect(snapshot.cacheKey, 'WO-1:${_sha('a')}');
      expect(rows, hasLength(1));
      expect(rows.single.principalScope, 'person-1');
      expect(rows.single.workOrderPublicId, 'WO-1');
      expect(rows.single.reportSha256, _sha('a'));
      expect(rows.single.sourceOverlaySha256, _sha('b'));
    },
  );

  test(
    'a new report hash invalidates the older snapshot for that job',
    () async {
      var reportChar = 'a';
      adapter.on('GET', '/api/v1/field/fiber/work-order-evidence-map', (_) {
        return (200, _response('WO-1', reportChar: reportChar));
      });

      await repo().fetch('WO-1');
      reportChar = 'c';
      await repo().fetch('WO-1');
      final rows = await db.select(db.cachedWorkOrderEvidenceMaps).get();

      expect(rows, hasLength(1));
      expect(rows.single.reportSha256, _sha('c'));
    },
  );

  test('offline fallback is explicit and never crosses work orders', () async {
    adapter.on('GET', '/api/v1/field/fiber/work-order-evidence-map', (_) {
      return (200, _response('WO-1', reportChar: 'a'));
    });
    await repo().fetch('WO-1');
    adapter.on('GET', '/api/v1/field/fiber/work-order-evidence-map', (_) {
      return (503, {'detail': 'offline'});
    });

    final cached = await repo().fetch('WO-1');

    expect(cached.fromCache, isTrue);
    expect(cached.cachedAt, isNotNull);
    expect(cached.cacheKey, 'WO-1:${_sha('a')}');
    await expectLater(repo().fetch('WO-2'), throwsA(isA<DioException>()));
  });

  test(
    'authoritative 4xx conflict never falls back to stale evidence',
    () async {
      adapter.on('GET', '/api/v1/field/fiber/work-order-evidence-map', (_) {
        return (200, _response('WO-1', reportChar: 'a'));
      });
      await repo().fetch('WO-1');
      adapter.on('GET', '/api/v1/field/fiber/work-order-evidence-map', (_) {
        return (409, {'detail': 'observation lineage conflict'});
      });

      await expectLater(repo().fetch('WO-1'), throwsA(isA<DioException>()));
    },
  );

  test('offline evidence never crosses authenticated principals', () async {
    adapter.on('GET', '/api/v1/field/fiber/work-order-evidence-map', (_) {
      return (200, _response('WO-1', reportChar: 'a'));
    });
    await repo().fetch('WO-1');
    await store.save(
      accessToken: fakeJwt(
        expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
        sub: 'person-2',
      ),
    );
    adapter.on('GET', '/api/v1/field/fiber/work-order-evidence-map', (_) {
      return (503, {'detail': 'offline'});
    });

    await expectLater(repo().fetch('WO-1'), throwsA(isA<DioException>()));
  });

  test(
    'mismatched work-order identity fails closed and is not cached',
    () async {
      adapter.on('GET', '/api/v1/field/fiber/work-order-evidence-map', (_) {
        return (200, _response('WO-2', reportChar: 'a'));
      });

      await expectLater(repo().fetch('WO-1'), throwsA(isA<FormatException>()));

      expect(await db.select(db.cachedWorkOrderEvidenceMaps).get(), isEmpty);
    },
  );

  test(
    'model preserves current, superseded, combined, and unrenderable facts',
    () {
      final snapshot = WorkOrderEvidenceMapSnapshot.fromJson(
        _response(
          'WO-1',
          reportChar: 'a',
          features: [
            _feature(1, context: 'current_source'),
            _feature(2, context: 'superseded_source'),
            _feature(3, context: 'current_and_superseded_source'),
            _feature(
              4,
              context: 'current_source',
              geometryState: 'source_geometry_unrenderable',
              geometry: {'type': 'GeometryCollection', 'geometries': const []},
            ),
          ],
        ),
        requestedWorkOrderPublicId: 'WO-1',
      );

      expect(snapshot.features.map((feature) => feature.context), {
        'current_source',
        'superseded_source',
        'current_and_superseded_source',
      });
      expect(snapshot.currentFeatureCount, 3);
      expect(snapshot.supersededFeatureCount, 2);
      expect(snapshot.unrenderableFeatureCount, 1);
      expect(snapshot.features.last.geometry.type, 'GeometryCollection');
      expect(snapshot.features.last.isRenderable, isFalse);
    },
  );

  test('model fails closed on observation-list and source-content drift', () {
    final wrongCount = _response('WO-1', reportChar: 'a');
    wrongCount['feature_collection']['features'][0]['properties']['work_order_evidence']['current_observation_count'] =
        2;
    expect(
      () => WorkOrderEvidenceMapSnapshot.fromJson(
        wrongCount,
        requestedWorkOrderPublicId: 'WO-1',
      ),
      throwsA(isA<FormatException>()),
    );

    final wrongContent = _response('WO-1', reportChar: 'a');
    wrongContent['feature_collection']['features'][0]['properties']['work_order_evidence']['current_observations'][0]['feature_content_sha256'] =
        _sha('f');
    expect(
      () => WorkOrderEvidenceMapSnapshot.fromJson(
        wrongContent,
        requestedWorkOrderPublicId: 'WO-1',
      ),
      throwsA(isA<FormatException>()),
    );

    final unclosedPolygon = _response('WO-1', reportChar: 'a');
    unclosedPolygon['feature_collection']['features'][0]['geometry'] = {
      'type': 'Polygon',
      'coordinates': [
        [
          [7.42, 9.02],
          [7.43, 9.02],
          [7.43, 9.03],
          [7.42, 9.03],
        ],
      ],
    };
    expect(
      () => WorkOrderEvidenceMapSnapshot.fromJson(
        unclosedPolygon,
        requestedWorkOrderPublicId: 'WO-1',
      ),
      throwsA(isA<FormatException>()),
    );
  });

  testWidgets(
    'screen renders only supplied evidence, distinctions, hashes, and cache warning',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(900, 2400));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final parsed = WorkOrderEvidenceMapSnapshot.fromJson(
        _response(
          'WO-1',
          reportChar: 'a',
          features: [
            _feature(1, context: 'current_source'),
            _feature(2, context: 'superseded_source'),
            _feature(
              3,
              context: 'current_and_superseded_source',
              geometryState: 'source_geometry_unrenderable',
              geometry: {'type': 'GeometryCollection', 'geometries': const []},
            ),
          ],
        ),
        requestedWorkOrderPublicId: 'WO-1',
        fromCache: true,
        cachedAt: DateTime.utc(2026, 7, 18, 8),
      );
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            workOrderEvidenceMapProvider(
              'WO-1',
            ).overrideWith((ref) async => parsed),
          ],
          child: MaterialApp(
            theme: lightTheme,
            home: const WorkOrderEvidenceMapScreen(
              workOrderPublicId: 'WO-1',
              showTiles: false,
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('cached-evidence-warning')), findsOneWidget);
      expect(
        find.byKey(const Key('exact-work-order-evidence-map')),
        findsOneWidget,
      );
      expect(find.byKey(const Key('current-source-count')), findsOneWidget);
      expect(find.byKey(const Key('superseded-source-count')), findsOneWidget);
      expect(
        find.byKey(const Key('unrenderable-feature-count')),
        findsOneWidget,
      );
      expect(find.text(_sha('a')), findsOneWidget);
      expect(find.text('Current source'), findsWidgets);
      expect(find.text('Superseded source'), findsWidgets);
      expect(find.text('Current and superseded source'), findsOneWidget);
      expect(
        find.text('Source geometry cannot be rendered unchanged'),
        findsOneWidget,
      );
    },
  );

  testWidgets(
    'empty snapshot does not infer unobserved assets or fault areas',
    (tester) async {
      final parsed = WorkOrderEvidenceMapSnapshot.fromJson(
        _response('WO-1', reportChar: 'a', features: const []),
        requestedWorkOrderPublicId: 'WO-1',
      );
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            workOrderEvidenceMapProvider(
              'WO-1',
            ).overrideWith((ref) async => parsed),
          ],
          child: MaterialApp(
            theme: lightTheme,
            home: const WorkOrderEvidenceMapScreen(
              workOrderPublicId: 'WO-1',
              showTiles: false,
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(
        find.text('No immutable fiber observations are attached to this job.'),
        findsOneWidget,
      );
      expect(
        find.text(
          'No unobserved assets or likely fault areas are inferred here.',
        ),
        findsOneWidget,
      );
    },
  );
}

String _sha(String character) => List.filled(64, character).join();

Map<String, dynamic> _response(
  String workOrderPublicId, {
  required String reportChar,
  List<Map<String, dynamic>>? features,
}) {
  final cohort = features ?? [_feature(1, context: 'current_source')];
  final currentCount = cohort.fold<int>(0, (count, feature) {
    final context = feature['properties']['work_order_evidence']['context'];
    return count + (context == 'superseded_source' ? 0 : 1);
  });
  final supersededCount = cohort.fold<int>(0, (count, feature) {
    final context = feature['properties']['work_order_evidence']['context'];
    return count + (context == 'current_source' ? 0 : 1);
  });
  for (final feature in cohort) {
    final evidence = feature['properties']['work_order_evidence'];
    evidence['work_order_public_id'] = workOrderPublicId;
    for (final field in ['current_observations', 'superseded_observations']) {
      for (final observation in evidence[field]) {
        observation['work_order_public_id'] = workOrderPublicId;
      }
    }
  }
  return {
    'report_sha256': _sha(reportChar),
    'source_overlay_sha256': _sha('b'),
    'worklist_report_sha256': _sha('c'),
    'observation_evidence_sha256': _sha('d'),
    'work_order_id': '11111111-1111-1111-1111-111111111111',
    'work_order_public_id': workOrderPublicId,
    'observation_count': currentCount + supersededCount,
    'current_source_observation_count': currentCount,
    'superseded_source_observation_count': supersededCount,
    'feature_count': cohort.length,
    'evidence_context_counts': const {},
    'geometry_presentation_counts': const {},
    'feature_collection': {'type': 'FeatureCollection', 'features': cohort},
    'schema_version': 1,
  };
}

Map<String, dynamic> _feature(
  int index, {
  required String context,
  String geometryState = 'exact_geojson',
  Map<String, dynamic>? geometry,
}) {
  final currentCount = context == 'superseded_source' ? 0 : 1;
  final supersededCount = context == 'current_source' ? 0 : 1;
  final contextLabel = switch (context) {
    'current_source' => 'Current source',
    'superseded_source' => 'Superseded source',
    _ => 'Current and superseded source',
  };
  final contextTone = switch (context) {
    'current_source' => 'positive',
    'superseded_source' => 'warning',
    _ => 'info',
  };
  return {
    'type': 'Feature',
    'id': 'feature-$index',
    'geometry':
        geometry ??
        {
          'type': 'Point',
          'coordinates': [7.4 + index / 1000, 9.0 + index / 1000],
        },
    'properties': {
      'display_name': 'FAT Evidence $index',
      'asset_type': 'fat',
      'source_system': 'crm.dotmac.io',
      'source_profile': 'crm-map-export',
      'external_id': 'FAT-$index',
      'content_sha256': _sha('a'),
      'geometry_sha256': _sha('b'),
      'map_feature_sha256': _sha('c'),
      'work_order_evidence_sha256': _sha('d'),
      'work_order_map_feature_sha256': _sha('e'),
      'geometry_presentation_state': geometryState,
      'geometry_presentation': {
        'value': geometryState,
        'label': geometryState == 'exact_geojson'
            ? 'Exact source geometry'
            : 'Source geometry cannot be rendered unchanged',
        'tone': geometryState == 'exact_geojson' ? 'positive' : 'warning',
        'icon': geometryState == 'exact_geojson' ? 'check' : 'alert',
      },
      'work_order_evidence': {
        'context': context,
        'context_presentation': {
          'value': context,
          'label': contextLabel,
          'tone': contextTone,
          'icon': context == 'superseded_source' ? 'clock' : 'check',
        },
        'current_observation_count': currentCount,
        'current_observations': [
          if (currentCount == 1) _observation(index, isCurrent: true),
        ],
        'superseded_observation_count': supersededCount,
        'superseded_observations': [
          if (supersededCount == 1) _observation(index, isCurrent: false),
        ],
        'work_order_id': '11111111-1111-1111-1111-111111111111',
        'work_order_public_id': 'WO-1',
      },
    },
  };
}

Map<String, dynamic> _observation(int index, {required bool isCurrent}) => {
  'observation_id': 'observation-$index-${isCurrent ? 'current' : 'old'}',
  'staged_feature_id': 'staged-$index-${isCurrent ? 'current' : 'old'}',
  'feature_content_sha256': _sha(isCurrent ? 'a' : 'f'),
  'claim_sha256': _sha('b'),
  'observation_sha256': _sha('c'),
  'verification_scope': 'presence',
  'outcome': 'agrees',
  'observed_at': '2026-07-18T08:00:00Z',
  'work_order_public_id': 'WO-1',
};
