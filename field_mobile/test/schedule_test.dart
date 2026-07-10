import 'package:dotmac_field/features/jobs/job_models.dart';
import 'package:dotmac_field/features/jobs/jobs_providers.dart';
import 'package:dotmac_field/features/schedule/schedule_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

JobSummary _job({
  required String id,
  required String title,
  DateTime? scheduledStart,
}) => JobSummary(
  id: id,
  title: title,
  status: 'dispatched',
  workType: 'install',
  priority: 'normal',
  scheduledStart: scheduledStart,
);

void main() {
  Widget app(List<JobSummary> jobs, {bool fromCache = false}) => ProviderScope(
    overrides: [
      allAssignedJobsProvider.overrideWith(
        (ref) async => JobList(jobs, fromCache: fromCache),
      ),
    ],
    child: const MaterialApp(home: ScheduleScreen()),
  );

  testWidgets('renders scheduled and unscheduled work', (tester) async {
    await tester.pumpWidget(
      app([
        _job(
          id: 'wo-1',
          title: 'Install fiber',
          scheduledStart: DateTime.now().add(const Duration(hours: 3)),
        ),
        _job(id: 'wo-2', title: 'Repair drop'),
      ]),
    );
    await tester.pumpAndSettle();

    expect(find.text('Install fiber'), findsOneWidget);
    expect(find.text('Repair drop'), findsOneWidget);
    expect(find.text('Unscheduled'), findsOneWidget);
  });

  testWidgets('empty schedule shows the calm empty state', (tester) async {
    await tester.pumpWidget(app([]));
    await tester.pumpAndSettle();
    expect(find.text('No assigned work yet'), findsOneWidget);
  });

  testWidgets('shows offline banner when served from cache', (tester) async {
    await tester.pumpWidget(
      app([_job(id: 'wo-1', title: 'Install fiber')], fromCache: true),
    );
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('schedule-offline-banner')), findsOneWidget);
  });

  testWidgets('no offline banner when fresh from network', (tester) async {
    await tester.pumpWidget(app([_job(id: 'wo-1', title: 'Install fiber')]));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('schedule-offline-banner')), findsNothing);
  });
}
