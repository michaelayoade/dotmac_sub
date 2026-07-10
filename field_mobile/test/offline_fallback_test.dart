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
import 'package:dotmac_field/features/jobs/jobs_providers.dart';
import 'package:drift/native.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqlite3/open.dart';

import 'helpers/fake_http.dart';

void main() {
  if (Platform.isLinux) {
    open.overrideFor(
      OperatingSystem.linux,
      () => DynamicLibrary.open('libsqlite3.so.0'),
    );
  }

  late AppDatabase db;
  late FakeHttpAdapter adapter;
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

  JobsRepository repo() => container.read(jobsRepositoryProvider);

  test('jobs list serves the network and warms the cache', () async {
    adapter.on(
      'GET',
      '/api/v1/field/jobs',
      (_) => (
        200,
        {
          'items': [
            {
              'id': 'wo-1',
              'title': 'Install',
              'status': 'dispatched',
              'work_type': 'install',
              'priority': 'normal',
            },
          ],
          'count': 1,
        },
      ),
    );
    final list = await repo().fetchJobs();
    expect(list.fromCache, isFalse);
    expect(list.jobs.single.id, 'wo-1');
    // Cache was warmed.
    expect((await db.select(db.cachedJobs).get()).length, 1);
  });

  test('jobs list falls back to cache when the network fails', () async {
    await sync.cacheJobs([
      {
        'id': 'wo-9',
        'title': 'Cached job',
        'status': 'in_progress',
        'work_type': 'repair',
        'priority': 'high',
      },
    ]);
    adapter.on('GET', '/api/v1/field/jobs', (_) => (503, {'detail': 'down'}));

    final list = await repo().fetchJobs();
    expect(list.fromCache, isTrue);
    expect(list.jobs.single.id, 'wo-9');
    expect(list.jobs.single.title, 'Cached job');
  });

  test('detail falls back to cached detail json when offline', () async {
    await sync.cacheJobs([
      {
        'id': 'wo-1',
        'title': 'Install',
        'status': 'dispatched',
        'work_type': 'install',
        'priority': 'normal',
      },
    ]);
    await sync.cacheJobDetail('wo-1', {
      'job': {
        'id': 'wo-1',
        'title': 'Install',
        'status': 'dispatched',
        'work_type': 'install',
        'priority': 'normal',
        'scheduled_start': null,
        'scheduled_end': null,
        'estimated_duration_minutes': null,
        'started_at': null,
        'completed_at': null,
        'description': null,
      },
      'location': {
        'latitude': null,
        'longitude': null,
        'address_text': '12 Road',
        'source': 'address_only',
      },
      'customer': null,
      'ticket_ref': null,
      'notes': [],
      'materials': [],
    });
    adapter.on(
      'GET',
      '/api/v1/field/jobs/wo-1',
      (_) => (503, {'detail': 'down'}),
    );

    final detail = await repo().fetchDetail('wo-1');
    expect(detail.job.id, 'wo-1');
    expect(detail.location.addressText, '12 Road');
  });

  test('rethrows when offline and nothing cached', () async {
    adapter.on('GET', '/api/v1/field/jobs', (_) => (503, {'detail': 'down'}));
    expect(repo().fetchJobs(), throwsA(isA<DioException>()));
  });
}
