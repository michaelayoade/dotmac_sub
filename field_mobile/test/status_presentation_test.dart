import 'package:dotmac_field/app/status_presentation.dart';
import 'package:dotmac_field/app/theme.dart';
import 'package:dotmac_field/app/widgets/status_pill.dart';
import 'package:dotmac_field/features/jobs/job_models.dart';
import 'package:dotmac_field/features/manager/manager_providers.dart';
import 'package:dotmac_field/features/today/map_models.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

const _presentationJson = <String, dynamic>{
  'value': 'paused',
  'label': 'Waiting for access',
  'tone': 'warning',
  'icon': 'alert',
};

void main() {
  test('job DTO consumes server presentation and keeps raw workflow value', () {
    final job = JobSummary.fromJson({
      'id': 'wo-1',
      'title': 'Repair drop',
      'status': 'paused',
      'status_presentation': _presentationJson,
      'work_type': 'repair',
      'priority': 'high',
    });

    expect(job.status, 'paused');
    expect(job.statusPresentation.label, 'Waiting for access');
    expect(job.statusPresentation.tone, StatusTone.warning);
    expect(job.statusPresentation.icon, 'alert');
  });

  test('older cached job payload falls back to a neutral presentation', () {
    final job = JobSummary.fromJson({
      'id': 'wo-2',
      'title': 'Survey',
      'status': 'awaiting_parts',
      'work_type': 'survey',
      'priority': 'normal',
    });

    expect(job.statusPresentation.label, 'Awaiting parts');
    expect(job.statusPresentation.tone, StatusTone.neutral);
    expect(job.statusPresentation.icon, 'info');
  });

  test('manager and map DTOs consume the same server contract', () {
    final managerJob = ManagerJob.fromJson({
      'id': 'wo-3',
      'title': 'Install',
      'status': 'paused',
      'status_presentation': _presentationJson,
      'priority': 'normal',
      'work_type': 'install',
    });
    final mapResult = MapPlaceSearchResult.fromJson({
      'kind': 'job',
      'id': 'wo-3',
      'title': 'Install',
      'status': 'paused',
      'status_presentation': _presentationJson,
      'latitude': 9.07,
      'longitude': 7.49,
    });

    expect(managerJob.statusPresentation.label, 'Waiting for access');
    expect(mapResult.statusPresentation?.tone, StatusTone.warning);
  });

  testWidgets('status pill renders the server label, icon, and semantic tone', (
    tester,
  ) async {
    const presentation = StatusPresentation(
      value: 'paused',
      label: 'Waiting for access',
      tone: StatusTone.warning,
      icon: 'alert',
    );
    await tester.pumpWidget(
      const MaterialApp(home: Scaffold(body: StatusPill(presentation))),
    );

    expect(find.text('WAITING FOR ACCESS'), findsOneWidget);
    final icon = tester.widget<Icon>(find.byIcon(Icons.warning_amber_rounded));
    expect(icon.color, AppColors.semanticWarning);
  });
}
