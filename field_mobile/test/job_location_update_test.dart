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
  setUpAll(() {
    if (Platform.isLinux) {
      open.overrideFor(
        OperatingSystem.linux,
        () => DynamicLibrary.open('libsqlite3.so.0'),
      );
    }
  });

  test(
    'updateLocation patches the API and refreshes cached detail location',
    () async {
      final adapter = FakeHttpAdapter();
      final store = InMemoryTokenStore();
      final dio = Dio(BaseOptions(baseUrl: 'https://test.local'));
      dio.httpClientAdapter = adapter;
      final api = ApiClient(
        baseUrl: 'https://test.local',
        tokenStore: store,
        dio: dio,
      );
      final db = AppDatabase(NativeDatabase.memory());
      final sync = SyncService(
        db: db,
        api: api,
        connectivity: FakeConnectivity(),
        throttle: Duration.zero,
      );
      final container = ProviderContainer(
        overrides: [
          apiClientProvider.overrideWithValue(api),
          syncServiceProvider.overrideWithValue(sync),
        ],
      );
      addTearDown(container.dispose);
      addTearDown(sync.dispose);
      addTearDown(db.close);

      await sync.cacheJobs([
        {
          'id': 'wo-1',
          'title': 'Install CPE',
          'status': 'dispatched',
          'work_type': 'install',
          'priority': 'normal',
          'scheduled_start': null,
        },
      ]);
      await sync.cacheJobDetail('wo-1', {
        'job': {
          'id': 'wo-1',
          'title': 'Install CPE',
          'status': 'dispatched',
          'work_type': 'install',
          'priority': 'normal',
        },
        'location': {
          'latitude': 6.5,
          'longitude': 3.4,
          'address_text': 'Old address',
          'source': 'geocoded',
        },
      });

      adapter.on('PATCH', '/api/v1/field/jobs/wo-1/location', (options) {
        final body = options.data as Map;
        expect(body['latitude'], 6.601);
        expect(body['longitude'], 3.501);
        return (
          200,
          {
            'location': {
              'latitude': 6.601,
              'longitude': 3.501,
              'address_text': 'Old address',
              'source': 'manual',
            },
          },
        );
      });

      final location = await container
          .read(jobsRepositoryProvider)
          .updateLocation(jobId: 'wo-1', latitude: 6.601, longitude: 3.501);

      expect(location.latitude, 6.601);
      expect(location.longitude, 3.501);
      expect(location.source, 'manual');

      final cached = await sync.readCachedDetail('wo-1');
      final cachedLocation = (cached!['location'] as Map)
          .cast<String, dynamic>();
      expect(cachedLocation['latitude'], 6.601);
      expect(cachedLocation['longitude'], 3.501);
      expect(cachedLocation['source'], 'manual');
    },
  );
}
