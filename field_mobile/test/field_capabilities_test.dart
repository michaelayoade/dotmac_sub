import 'dart:async';

import 'package:dotmac_field/app/app.dart';
import 'package:dotmac_field/app/router.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/features/attendance/attendance_repository.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/jobs/job_models.dart';
import 'package:dotmac_field/features/jobs/jobs_providers.dart';
import 'package:dotmac_field/features/location/location_tracking_controller.dart';
import 'package:dotmac_field/features/manager/manager_providers.dart';
import 'package:dotmac_field/features/profile/profile_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support/field_capability_fixtures.dart';

class _VendorController extends AuthController {
  @override
  AuthState build() => const Authenticated(LoginMode.vendor);
}

void main() {
  test('capabilities require exact current server availability evidence', () {
    expect(
      FieldExecutionCapabilities.fromJson(null).materials.available,
      isFalse,
    );
    final caps = FieldExecutionCapabilities.fromJson({
      'attendance': {'available': 'true'},
      'materials': {'available': true},
      'expenses': {'available': false, 'reason': 'Vendor workflow unavailable'},
    });
    expect(caps.attendance.available, isFalse);
    expect(caps.locationTracking.available, isFalse);
    expect(caps.materials.available, isTrue);
    expect(caps.expenses.available, isFalse);
    expect(caps.expenses.reason, 'Vendor workflow unavailable');
  });

  test(
    'loading and failed capability refresh discard previous authorization',
    () async {
      var response = Completer<MeSummary>();
      final container = ProviderContainer(
        overrides: [meProvider.overrideWith((ref) => response.future)],
      );
      addTearDown(container.dispose);
      final subscription = container.listen(
        fieldCapabilitiesProvider,
        (_, _) {},
      );
      addTearDown(subscription.close);
      expect(
        container.read(fieldCapabilitiesProvider).attendance.available,
        isFalse,
      );
      response.complete(
        const MeSummary(
          name: 'Tech',
          openJobs: 0,
          completedToday: 0,
          capabilities: availableFieldCapabilities,
        ),
      );
      await container.read(meProvider.future);
      expect(
        container.read(fieldCapabilitiesProvider).attendance.available,
        isTrue,
      );
      response = Completer<MeSummary>();
      container.invalidate(meProvider);
      expect(
        container.read(fieldCapabilitiesProvider).attendance.available,
        isFalse,
      );
      response.completeError(StateError('Capabilities unavailable'));
      await expectLater(container.read(meProvider.future), throwsStateError);
      expect(
        container.read(fieldCapabilitiesProvider).attendance.available,
        isFalse,
      );
    },
  );

  testWidgets(
    'vendor core shell makes no attendance request and guards every ancillary deep link',
    (tester) async {
      var attendanceReads = 0;
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            authControllerProvider.overrideWith(_VendorController.new),
            meProvider.overrideWith(
              (ref) async => const MeSummary(
                name: 'Vendor Crew',
                openJobs: 0,
                completedToday: 0,
              ),
            ),
            managerProfileProvider.overrideWith((ref) async => null),
            todayJobsProvider.overrideWith(
              (ref) async => const JobList(<JobSummary>[]),
            ),
            pendingOutboxProvider.overrideWith(
              (ref) => Stream.value(<OutboxEntry>[]),
            ),
            conflictOutboxProvider.overrideWith(
              (ref) => Stream.value(<OutboxEntry>[]),
            ),
            pendingPhotosProvider.overrideWith((ref) => Stream.value(0)),
            attendanceRepositoryProvider.overrideWith((ref) {
              attendanceReads++;
              throw StateError('Vendor attendance must never be requested');
            }),
          ],
          child: const DotmacFieldApp(),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('Today'), findsOneWidget);
      expect(find.text('Schedule'), findsOneWidget);
      expect(find.text('Materials'), findsNothing);
      expect(find.text('Expenses'), findsNothing);
      expect(find.byType(LocationTrackingHost), findsNothing);
      expect(find.byType(LocationSharingControls), findsNothing);
      expect(attendanceReads, 0);
      final container = ProviderScope.containerOf(
        tester.element(find.byType(NavigationBar)),
      );
      final router = container.read(routerProvider);
      for (final path in [
        '/materials',
        '/materials/new',
        '/materials/one',
        '/expenses',
        '/expenses/new',
        '/expenses/one',
        '/manager/expenses/one',
        '/jobs/one/chat',
        '/jobs/one/fiber-evidence',
      ]) {
        router.go(path);
        await tester.pumpAndSettle();
        expect(find.text('Feature unavailable'), findsOneWidget, reason: path);
        expect(tester.takeException(), isNull, reason: path);
      }
      expect(attendanceReads, 0);
    },
  );
}
