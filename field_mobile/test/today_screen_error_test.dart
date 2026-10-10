import 'support/field_capability_fixtures.dart';
import 'package:dio/dio.dart';
import 'package:dotmac_field/app/theme.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/connectivity.dart';
import 'package:dotmac_field/core/offline/sync_service.dart';
import 'package:dotmac_field/features/attendance/attendance_models.dart';
import 'package:dotmac_field/features/attendance/attendance_repository.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/execution/execution_controller.dart';
import 'package:dotmac_field/features/jobs/jobs_providers.dart';
import 'package:dotmac_field/features/profile/profile_screen.dart';
import 'package:dotmac_field/features/today/today_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fake_http.dart';
import 'helpers/secure_store.dart';

class _AttendanceController extends AttendanceController {
  @override
  Future<AttendanceView> build() async => const AttendanceView(
    state: AttendanceState.notCheckedIn,
    attendanceDate: '2026-10-10',
    timezone: 'Africa/Lagos',
    allowedActions: {AttendanceAction.checkIn},
  );
}

void main() {
  useHostSqlite3();

  testWidgets('Today renders recovery UI for failed jobs with an empty cache', (
    tester,
  ) async {
    // Keep the real repository and Today provider: a failed read with no saved
    // jobs must reach AsyncError, rather than a successful empty JobList.
    final device = newTestDevice();
    final store = await tester.runAsync(() => device.open(techOne));
    final adapter = FakeHttpAdapter()
      ..on('GET', '/api/v1/field/jobs', (_) => (404, {'detail': 'Not found'}));
    final tokens = InMemoryTokenStore();
    await tokens.save(
      accessToken: fakeJwt(
        expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
      ),
      refreshToken: 'test-refresh',
      loginMode: LoginMode.staff,
    );
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'))
      ..httpClientAdapter = adapter;
    final api = ApiClient(
      baseUrl: 'https://test.local',
      tokenStore: tokens,
      dio: dio,
    );
    final sync = SyncService(
      db: store!.database,
      api: api,
      connectivity: FakeConnectivity(),
      evidence: store.evidence,
      delay: (_) async {},
    );
    final container = ProviderContainer(
      overrides: [
        apiClientProvider.overrideWithValue(api),
        syncServiceProvider.overrideWithValue(sync),
        meProvider.overrideWith(
          (ref) async => const MeSummary(
            name: 'Test technician',
            capabilities: availableFieldCapabilities,
            openJobs: 0,
            completedToday: 0,
          ),
        ),
        attendanceControllerProvider.overrideWith(_AttendanceController.new),
        locationReminderShownProvider.overrideWith((ref) => true),
        pendingOutboxProvider.overrideWith((ref) => Stream.value([])),
        conflictOutboxProvider.overrideWith((ref) => Stream.value([])),
        pendingPhotosProvider.overrideWith((ref) => Stream.value(0)),
      ],
    );
    addTearDown(() async {
      container.dispose();
      await sync.dispose();
      await device.dispose();
      dio.close();
    });

    await tester.runAsync(() async {
      expect(await sync.readCachedJobs(), isEmpty);
      await expectLater(
        container.read(todayJobsProvider.future),
        throwsA(
          isA<DioException>().having(
            (error) => error.response?.statusCode,
            'status',
            404,
          ),
        ),
      );
    });
    expect(container.read(todayJobsProvider).hasError, isTrue);

    await tester.pumpWidget(
      UncontrolledProviderScope(
        container: container,
        child: MaterialApp(theme: lightTheme, home: const TodayScreen()),
      ),
    );
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    expect(find.byType(ErrorWidget), findsNothing);
    expect(find.text('Could not load jobs — pull to retry'), findsOneWidget);
    expect(find.byType(RefreshIndicator), findsOneWidget);
    expect(find.text('No jobs in this view'), findsNothing);
    expect(find.byKey(const Key('offline-banner')), findsNothing);
  });
}
