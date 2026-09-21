import 'package:dotmac_field/features/manager/manager_dispatch_detail_screen.dart';
import 'package:dotmac_field/features/manager/manager_providers.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

const _manager = ManagerProfile(
  name: 'Amaka Manager',
  roles: ['field_manager'],
  permissions: ['operations:work_order:read'],
  isManager: true,
);

Widget _subject({
  List<ManagerJob> jobs = const [],
  ManagerProfile? profile = _manager,
  Object? jobsError,
}) {
  return ProviderScope(
    overrides: [
      managerProfileProvider.overrideWith((ref) async => profile),
      managerJobsProvider.overrideWith((ref) async {
        if (jobsError != null) throw jobsError;
        return jobs;
      }),
    ],
    child: const MaterialApp(
      home: ManagerDispatchDetailScreen(jobId: 'wo-detail'),
    ),
  );
}

void main() {
  testWidgets('shows a stable unavailable state when dispatch is not open', (
    tester,
  ) async {
    await tester.pumpWidget(_subject());
    await tester.pumpAndSettle();

    expect(find.text('Dispatch no longer available'), findsOneWidget);
    expect(
      find.text('This work order is no longer in the open dispatch queue.'),
      findsOneWidget,
    );
  });

  testWidgets('fails closed without manager work-order read permission', (
    tester,
  ) async {
    await tester.pumpWidget(
      _subject(
        profile: const ManagerProfile(
          name: 'Restricted Manager',
          roles: ['field_manager'],
          permissions: [],
          isManager: true,
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(
      find.text('You do not have permission to view dispatch details.'),
      findsOneWidget,
    );
  });

  testWidgets('shows a retryable load failure', (tester) async {
    await tester.pumpWidget(
      _subject(jobsError: StateError('dispatch unavailable')),
    );
    await tester.pumpAndSettle();

    expect(find.text('Could not load this dispatch'), findsOneWidget);
    expect(find.text('Retry'), findsOneWidget);
  });

  testWidgets('detail layout does not overflow a narrow mobile viewport', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(320, 568);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(
      _subject(
        jobs: [
          ManagerJob(
            id: 'wo-detail',
            title: 'Replace a damaged customer distribution cable',
            status: 'dispatched',
            priority: 'high',
            workType: 'emergency_repair',
            assignedToLabel: 'Ada Technician',
            subscriberLabel: 'Amina Bello (SUB-00000042)',
            addressText: '14 Very Long Garki Road, Central Business District',
            latitude: 9.0579,
            longitude: 7.4951,
            description:
                'Replace the damaged cable, test optical levels, and document '
                'the completed repair before leaving the site.',
          ),
        ],
      ),
    );
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    expect(find.text('Dispatch details'), findsOneWidget);
    expect(
      find.text('Replace a damaged customer distribution cable'),
      findsOneWidget,
    );
  });
}
