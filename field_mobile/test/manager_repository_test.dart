import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/manager/manager_providers.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fake_http.dart';

void main() {
  late ProviderContainer container;
  late FakeHttpAdapter adapter;

  setUp(() async {
    adapter = FakeHttpAdapter();
    final store = InMemoryTokenStore();
    await store.save(
      accessToken: fakeJwt(
        expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
      ),
      refreshToken: 'refresh',
    );
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'))
      ..httpClientAdapter = adapter;
    final client = ApiClient(
      baseUrl: 'https://test.local',
      tokenStore: store,
      dio: dio,
    );
    container = ProviderContainer(
      overrides: [apiClientProvider.overrideWithValue(client)],
    );
  });

  tearDown(() => container.dispose());

  test('unassignJob sends the assignment queue transition', () async {
    adapter.on('POST', '/api/v1/field/manager/assignments/queue-1/unassign', (
      options,
    ) {
      expect(options.data, {
        'reason': 'Manager unassigned technician from mobile dispatch',
      });
      return (200, {'id': 'queue-1', 'status': 'skipped'});
    });

    final outcome = await container
        .read(managerRepositoryProvider)
        .unassignJob(
          assignmentQueueId: 'queue-1',
          reason: 'Manager unassigned technician from mobile dispatch',
        );

    expect(outcome.id, 'queue-1');
    expect(outcome.status, 'skipped');
  });

  test(
    'fetchTechnicianLocationDetail reads the typed address detail',
    () async {
      adapter.on(
        'GET',
        '/api/v1/field/manager/team-map/tech-1/location-detail',
        (options) {
          return (
            200,
            {
              'position': {
                'technician_id': 'tech-1',
                'person_id': 'person-1',
                'label': 'Ada Technician',
                'status': 'on_shift',
                'latitude': 6.5244,
                'longitude': 3.3792,
                'accuracy_m': 8,
                'last_location_at': '2026-09-10T10:00:00Z',
                'is_live': true,
              },
              'address_text': 'Marina Road, Lagos',
              'address_status': 'available',
            },
          );
        },
      );

      final detail = await container
          .read(managerRepositoryProvider)
          .fetchTechnicianLocationDetail(technicianId: 'tech-1');

      expect(detail.position.technicianId, 'tech-1');
      expect(detail.position.latitude, 6.5244);
      expect(detail.addressText, 'Marina Road, Lagos');
      expect(detail.addressStatus, ManagerLocationAddressStatus.available);
    },
  );

  test('fetchJobs reads manager dispatch detail fields', () async {
    adapter.on('GET', '/api/v1/field/manager/jobs', (options) {
      expect(options.queryParameters, {'assigned_to_person_id': 'person-1'});
      return (
        200,
        {
          'items': [
            {
              'id': 'wo-dispatch-1',
              'title': 'Install service at Garki',
              'description': 'Replace the drop cable and commission the ONT.',
              'status': 'dispatched',
              'priority': 'high',
              'work_type': 'install',
              'assigned_to_label': 'Ada Technician',
              'subscriber_label': 'Amina Bello (SUB-42)',
              'address_text': '14 Garki Road, Abuja',
              'latitude': 9.0579,
              'longitude': 7.4951,
            },
          ],
        },
      );
    });

    final jobs = await container
        .read(managerRepositoryProvider)
        .fetchJobs(assignedToPersonId: 'person-1');

    expect(jobs, hasLength(1));
    expect(jobs.single.id, 'wo-dispatch-1');
    expect(
      jobs.single.description,
      'Replace the drop cable and commission the ONT.',
    );
    expect(jobs.single.assignedToLabel, 'Ada Technician');
    expect(jobs.single.addressText, '14 Garki Road, Abuja');
  });
}
