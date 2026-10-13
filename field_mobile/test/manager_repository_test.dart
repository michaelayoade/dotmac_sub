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

  for (final generation in <String?>[
    null,
    '36abc9bb-ffef-4453-bf98-497f61004836',
  ]) {
    test(
      'assignJob preserves snapshot evidence for generation $generation',
      () async {
        final job = ManagerJob.fromJson({
          'id': 'wo-1',
          'revision': '2026-10-11T09:12:13.123456Z',
          'assignment_generation': generation,
        });
        final command = ManagerJobAssignmentCommand.fromSnapshot(
          job: job,
          personId: 'person-1',
        );
        expect(
          command.commandId,
          matches(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
          ),
        );
        adapter.on('POST', '/api/v1/field/manager/jobs/wo-1/assign', (options) {
          expect(options.data, {
            'person_id': 'person-1',
            'expected_revision': '2026-10-11T09:12:13.123456Z',
            'expected_assignment_generation': generation,
            'command_id': command.commandId,
          });
          return (200, {});
        });
        final repository = container.read(managerRepositoryProvider);
        await repository.assignJob(command);
        // Sending the same explicit command retains its receipt identity.
        await repository.assignJob(command);
        expect(adapter.requests, hasLength(2));
        expect(
          ManagerJobAssignmentCommand.fromSnapshot(
            job: job,
            personId: 'person-1',
          ).commandId,
          isNot(command.commandId),
        );
      },
    );
  }

  test(
    'assignment without a valid server revision fails before any request',
    () {
      for (final revision in <String?>[null, 'invalid']) {
        final job = ManagerJob.fromJson({'id': 'wo-1', 'revision': revision});
        expect(
          () => ManagerJobAssignmentCommand.fromSnapshot(
            job: job,
            personId: 'person-1',
          ),
          throwsA(isA<ManagerAssignmentSnapshotUnavailable>()),
        );
      }
      expect(adapter.requests, isEmpty);
    },
  );

  test(
    'stale assignment surfaces conflict without retrying or changing evidence',
    () async {
      final command = ManagerJobAssignmentCommand.fromSnapshot(
        job: ManagerJob.fromJson({
          'id': 'wo-1',
          'revision': '2026-10-11T09:12:13.123456Z',
          'assignment_generation': '36abc9bb-ffef-4453-bf98-497f61004836',
        }),
        personId: 'person-1',
      );
      adapter.on('POST', '/api/v1/field/manager/jobs/wo-1/assign', (options) {
        return (409, {'detail': 'Work order changed; reload before assigning'});
      });
      await expectLater(
        container.read(managerRepositoryProvider).assignJob(command),
        throwsA(
          isA<DioException>().having(
            (error) => error.response?.statusCode,
            'status',
            409,
          ),
        ),
      );
      expect(adapter.requests, hasLength(1));
      expect(
        (adapter.requests.single.data as Map)['command_id'],
        command.commandId,
      );
    },
  );

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
              'revision': '2026-10-11T09:12:13.123456Z',
              'assignment_generation': '36abc9bb-ffef-4453-bf98-497f61004836',
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
      jobs.single.revision,
      DateTime.utc(2026, 10, 11, 9, 12, 13, 123, 456),
    );
    expect(
      jobs.single.assignmentGeneration,
      '36abc9bb-ffef-4453-bf98-497f61004836',
    );
    expect(
      jobs.single.description,
      'Replace the drop cable and commission the ONT.',
    );
    expect(jobs.single.assignedToLabel, 'Ada Technician');
    expect(jobs.single.addressText, '14 Garki Road, Abuja');
  });
}
