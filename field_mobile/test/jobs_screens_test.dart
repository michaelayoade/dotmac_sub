import 'package:dotmac_field/app/theme.dart';
import 'package:dotmac_field/features/jobs/job_detail_screen.dart';
import 'package:dotmac_field/features/jobs/job_models.dart';
import 'package:dotmac_field/features/jobs/jobs_providers.dart';
import 'package:dotmac_field/features/jobs/widgets/job_card.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

JobSummary _job({String status = 'dispatched', String workType = 'install'}) =>
    JobSummary(
      id: 'wo-1',
      title: 'Install fiber — Adaeze Okafor',
      status: status,
      workType: workType,
      priority: 'normal',
      scheduledStart: DateTime.utc(2026, 6, 10, 9),
      estimatedDurationMinutes: 90,
    );

JobDetail _detail({
  String status = 'dispatched',
  JobLocation? location,
  List<Map<String, dynamic>> materialRequests = const [],
  List<Map<String, dynamic>> history = const [],
}) => JobDetail(
  job: _job(status: status),
  location:
      location ??
      const JobLocation(
        latitude: 6.43,
        longitude: 3.42,
        addressText: '12 Admiralty Way',
        source: 'geocoded',
      ),
  customer: const JobCustomer(
    name: 'Adaeze Okafor',
    phone: '+2348012345678',
    servicePlan: '100 Mbps',
  ),
  ticketRef: 'TCK-1001',
  materialRequests: materialRequests,
  history: history,
);

Widget _wrap(Widget child, {List<Override> overrides = const []}) =>
    ProviderScope(
      overrides: overrides,
      child: MaterialApp(theme: lightTheme, home: child),
    );

void main() {
  testWidgets('job card shows work-type color bar and status dot', (
    tester,
  ) async {
    await tester.pumpWidget(_wrap(JobCard(job: _job())));

    expect(find.text('INSTALL'), findsOneWidget);
    expect(find.text('Install fiber — Adaeze Okafor'), findsOneWidget);
    expect(find.text('dispatched'), findsOneWidget);
    expect(find.text('~90 min'), findsOneWidget);

    final bar = tester
        .widgetList<Container>(find.byType(Container))
        .firstWhere(
          (c) =>
              c.constraints?.maxWidth == 5 ||
              c.color == AppColors.workType('install'),
          orElse: () => tester.widget<Container>(find.byType(Container).first),
        );
    expect(bar.color, AppColors.workType('install'));
  });

  group('action bar shows work actions per status', () {
    for (final (status, expected) in [
      ('scheduled', ['En Route', 'Arrived', 'Start Work']),
      ('dispatched', ['En Route', 'Arrived', 'Start Work']),
      ('in_progress', ['En Route', 'Arrived', 'Pause Work', 'Complete Work']),
      ('paused', ['En Route', 'Arrived', 'Resume Work']),
    ]) {
      testWidgets(status, (tester) async {
        final detail = _detail(status: status);
        await tester.pumpWidget(
          _wrap(
            const SizedBox(),
            overrides: [
              jobDetailProvider('wo-1').overrideWith((ref) async => detail),
            ],
          ),
        );
        await tester.pumpWidget(
          _wrap(
            const JobDetailScreen(jobId: 'wo-1'),
            overrides: [
              jobDetailProvider('wo-1').overrideWith((ref) async => detail),
            ],
          ),
        );
        await tester.pumpAndSettle();

        for (final label in expected) {
          expect(find.text(label), findsOneWidget);
        }
      });
    }

    testWidgets('completed jobs have no work action', (tester) async {
      final detail = _detail(status: 'completed');
      await tester.pumpWidget(
        _wrap(
          const JobDetailScreen(jobId: 'wo-1'),
          overrides: [
            jobDetailProvider('wo-1').overrideWith((ref) async => detail),
          ],
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('work-action-start')), findsNothing);
      expect(find.byKey(const Key('work-action-pause')), findsNothing);
      expect(find.byKey(const Key('work-action-resume')), findsNothing);
      expect(find.byKey(const Key('work-action-complete')), findsNothing);
    });
  });

  testWidgets('navigate button launches geo uri from coordinates', (
    tester,
  ) async {
    final launched = <Uri>[];
    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider('wo-1').overrideWith((ref) async => _detail()),
          uriLauncherProvider.overrideWithValue((uri) async {
            launched.add(uri);
            return true;
          }),
        ],
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('navigate-button')));
    expect(launched.single.toString(), 'geo:6.43,3.42?q=6.43,3.42');
  });

  testWidgets('address-only location falls back to maps text search', (
    tester,
  ) async {
    final launched = <Uri>[];
    const location = JobLocation(
      addressText: '12 Admiralty Way, Lekki',
      source: 'address_only',
    );
    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider(
            'wo-1',
          ).overrideWith((ref) async => _detail(location: location)),
          uriLauncherProvider.overrideWithValue((uri) async {
            launched.add(uri);
            return true;
          }),
        ],
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('navigate-button')));
    expect(launched.single.toString(), contains('q=12%20Admiralty%20Way'));
  });

  test('maps uri is null when nothing is known', () {
    const location = JobLocation(source: 'none');
    expect(location.mapsUri, isNull);
  });

  test('job detail accepts paginated notes and ignores malformed entries', () {
    final detail = JobDetail.fromJson({
      'job': {
        'id': 'wo-1',
        'title': 'Install',
        'status': 'dispatched',
        'work_type': 'install',
        'priority': 'normal',
      },
      'location': {'source': 'none'},
      'notes': {
        'items': [
          {'text': 'Stored note returned as text'},
          null,
          'unexpected',
        ],
      },
      'history': {
        'items': [
          {'type': 'note', 'title': 'History note'},
        ],
      },
      'materials': null,
    });

    expect(detail.notes, hasLength(1));
    expect(detail.notes.single['text'], 'Stored note returned as text');
    expect(detail.history, hasLength(1));
    expect(detail.history.single['title'], 'History note');
  });

  testWidgets('job detail renders notes returned with alternate body key', (
    tester,
  ) async {
    final detail = JobDetail(
      job: _job(),
      location: const JobLocation(source: 'none'),
      notes: const [
        {
          'text': 'Stored note returned as text',
          'author_name': 'Adaeze Okafor',
        },
      ],
    );
    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider('wo-1').overrideWith((ref) async => detail),
        ],
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Stored note returned as text'), findsOneWidget);
    expect(find.text('Adaeze Okafor'), findsOneWidget);
  });

  testWidgets('call button dials the customer', (tester) async {
    final launched = <Uri>[];
    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider('wo-1').overrideWith((ref) async => _detail()),
          uriLauncherProvider.overrideWithValue((uri) async {
            launched.add(uri);
            return true;
          }),
        ],
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('call-button')));
    expect(launched.single.scheme, 'tel');
  });

  testWidgets('job detail app bar offers material and expense requests', (
    tester,
  ) async {
    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider('wo-1').overrideWith((ref) async => _detail()),
        ],
      ),
    );
    await tester.pumpAndSettle();

    expect(find.byTooltip('Request materials'), findsOneWidget);
    expect(find.byTooltip('Request expense'), findsOneWidget);
  });

  testWidgets('job detail shows linked material requests', (tester) async {
    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider('wo-1').overrideWith(
            (ref) async => _detail(
              materialRequests: const [
                {
                  'id': 'mr-1',
                  'number': 'MR-1001',
                  'status': 'submitted',
                  'items': [
                    {'id': 'item-1'},
                  ],
                },
              ],
            ),
          ),
        ],
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Material requests'), findsOneWidget);
    expect(find.text('MR-1001'), findsOneWidget);
    expect(find.text('submitted · 1 item'), findsOneWidget);
  });

  testWidgets('job detail shows combined history activity', (tester) async {
    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider('wo-1').overrideWith(
            (ref) async => _detail(
              history: const [
                {
                  'id': 'mr:1',
                  'type': 'material_request',
                  'title': 'Material request MR-1001',
                  'description': 'submitted · 1 item',
                  'occurred_at': '2026-06-10T09:00:00Z',
                  'status': 'submitted',
                },
                {
                  'id': 'note:1',
                  'type': 'note',
                  'title': 'Internal note',
                  'description': 'Checked signal at cabinet',
                  'occurred_at': '2026-06-10T09:05:00Z',
                  'actor_name': 'Adaeze Okafor',
                  'is_internal': true,
                },
                {
                  'id': 'event:1',
                  'type': 'work_event',
                  'title': 'Work started',
                  'occurred_at': '2026-06-10T09:10:00Z',
                  'status': 'start',
                },
              ],
            ),
          ),
        ],
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('History'), findsOneWidget);
    expect(find.text('Material request MR-1001'), findsOneWidget);
    expect(find.text('submitted · 1 item'), findsOneWidget);
    expect(find.text('Checked signal at cabinet'), findsOneWidget);
    expect(find.text('Internal'), findsOneWidget);
    expect(find.text('Work started'), findsOneWidget);
  });

  testWidgets('technician can open add note composer from job detail', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider('wo-1').overrideWith((ref) async => _detail()),
        ],
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('add-note-action')));
    await tester.pumpAndSettle();

    expect(find.text('Add note'), findsWidgets);
    expect(find.byKey(const Key('note-body-field')), findsOneWidget);
    expect(find.byKey(const Key('internal-note-checkbox')), findsOneWidget);
    expect(find.text('Visible to staff only'), findsOneWidget);
    expect(find.byKey(const Key('save-note-action')), findsOneWidget);
    expect(
      tester.getTopLeft(find.byKey(const Key('note-body-field'))).dy,
      greaterThanOrEqualTo(0),
    );
    expect(
      tester.getBottomRight(find.byKey(const Key('note-body-field'))).dy,
      lessThan(tester.view.physicalSize.height),
    );
  });
}
