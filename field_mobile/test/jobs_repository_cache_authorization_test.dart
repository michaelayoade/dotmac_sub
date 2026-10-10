import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/connectivity.dart';
import 'package:dotmac_field/core/offline/sync_service.dart';
import 'package:dotmac_field/core/secure/secure_field_store.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/execution/execution_controller.dart';
import 'package:dotmac_field/features/jobs/jobs_providers.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fake_http.dart';
import 'helpers/secure_store.dart';

void main() {
  setUpAll(useHostSqlite3);
  late SecureFieldStore store;
  late FakeHttpAdapter adapter;
  late SyncService sync;
  late ProviderContainer container;
  const job = {
    'id': 'job-1',
    'title': 'Assigned visit',
    'status': 'scheduled',
    'work_type': 'installation',
    'priority': 'normal',
  };
  const detail = {'job': job, 'location': <String, Object?>{}};

  setUp(() async {
    store = await openTestStore();
    adapter = FakeHttpAdapter();
    final tokens = InMemoryTokenStore();
    await tokens.save(
      accessToken: fakeJwt(
        expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
      ),
      refreshToken: 'refresh',
      loginMode: LoginMode.vendor,
    );
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'));
    dio.httpClientAdapter = adapter;
    final refreshDio = Dio(BaseOptions(baseUrl: 'https://test.local'));
    refreshDio.httpClientAdapter = adapter;
    final api = ApiClient(
      baseUrl: 'https://test.local',
      tokenStore: tokens,
      dio: dio,
      refreshDio: refreshDio,
    );
    sync = SyncService(
      db: store.database,
      api: api,
      connectivity: FakeConnectivity(),
      evidence: store.evidence,
      delay: (_) async {},
    );
    container = ProviderContainer(
      overrides: [
        apiClientProvider.overrideWithValue(api),
        syncServiceProvider.overrideWithValue(sync),
      ],
    );
    adapter.on(
      'GET',
      '/api/v1/field/jobs',
      (_) => (
        200,
        {
          'items': [job],
        },
      ),
    );
    adapter.on('GET', '/api/v1/field/jobs/job-1', (_) => (200, detail));
    adapter.on(
      'POST',
      '/api/v1/auth/refresh',
      (_) => (401, {'detail': 'Revoked'}),
    );
    final repository = container.read(jobsRepositoryProvider);
    await repository.fetchJobs();
    await repository.fetchDetail('job-1');
  });

  tearDown(() async {
    container.dispose();
    await sync.dispose();
    await store.database.close();
  });

  for (final status in [400, 401, 403, 404, 409, 422]) {
    test('warm job cache cannot conceal HTTP $status', () async {
      adapter.on(
        'GET',
        '/api/v1/field/jobs',
        (_) => (status, {'detail': 'Denied'}),
      );
      adapter.on(
        'GET',
        '/api/v1/field/jobs/job-1',
        (_) => (status, {'detail': 'Denied'}),
      );
      final repository = container.read(jobsRepositoryProvider);
      await expectLater(repository.fetchJobs(), throwsA(isA<DioException>()));
      await expectLater(
        repository.fetchDetail('job-1'),
        throwsA(isA<DioException>()),
      );
    });
  }

  test('server outage preserves offline job access', () async {
    adapter.on(
      'GET',
      '/api/v1/field/jobs',
      (_) => (503, {'detail': 'Unavailable'}),
    );
    adapter.on(
      'GET',
      '/api/v1/field/jobs/job-1',
      (_) => (503, {'detail': 'Unavailable'}),
    );
    final repository = container.read(jobsRepositoryProvider);
    final jobs = await repository.fetchJobs();
    expect(jobs.fromCache, isTrue);
    expect(jobs.jobs.single.id, 'job-1');
    expect((await repository.fetchDetail('job-1')).job.id, 'job-1');
  });

  test('connection failure preserves offline job access', () async {
    (int, Object) offline(RequestOptions options) => throw DioException(
      requestOptions: options,
      type: DioExceptionType.connectionError,
    );
    adapter.on('GET', '/api/v1/field/jobs', offline);
    adapter.on('GET', '/api/v1/field/jobs/job-1', offline);
    final repository = container.read(jobsRepositoryProvider);
    expect((await repository.fetchJobs()).fromCache, isTrue);
    expect((await repository.fetchDetail('job-1')).job.id, 'job-1');
  });

  test('authoritative empty assignment response stays empty', () async {
    adapter.on('GET', '/api/v1/field/jobs', (_) => (200, {'items': []}));
    final jobs = await container.read(jobsRepositoryProvider).fetchJobs();
    expect(jobs.jobs, isEmpty);
    expect(jobs.fromCache, isFalse);
  });
}
