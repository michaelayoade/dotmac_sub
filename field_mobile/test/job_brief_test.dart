import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/jobs/job_brief_models.dart';
import 'package:dotmac_field/features/jobs/job_brief_panel.dart';
import 'package:dotmac_field/features/jobs/job_brief_repository.dart';
import 'package:dotmac_field/features/jobs/job_detail_screen.dart'
    show uriLauncherProvider;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fake_http.dart';

Map<String, dynamic> fixture() =>
    (jsonDecode(File('test/fixtures/job_brief.json').readAsStringSync()) as Map)
        .cast<String, dynamic>();

void main() {
  test(
    'repository sends issued scope and revision-pinned pagination',
    () async {
      final adapter = FakeHttpAdapter();
      adapter.on('GET', '/api/v1/field/jobs/brief-fixture/brief', (options) {
        expect(options.queryParameters, {
          'audience_scope': 'issued',
          'audience_link_id': 'link',
          'expected_revision_id': 'revision',
          'after_subscription_id': 'last',
          'limit': 25,
        });
        return (200, fixture());
      });
      final tokens = InMemoryTokenStore();
      await tokens.save(
        accessToken: fakeJwt(
          expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
        ),
        refreshToken: 'r',
        loginMode: LoginMode.staff,
      );
      final dio = Dio(BaseOptions(baseUrl: 'https://test.local'))
        ..httpClientAdapter = adapter;
      final client = ApiClient(
        baseUrl: 'https://test.local',
        tokenStore: tokens,
        dio: dio,
      );
      final container = ProviderContainer(
        overrides: [apiClientProvider.overrideWithValue(client)],
      );
      addTearDown(container.dispose);
      final brief = await container
          .read(jobBriefRepositoryProvider)
          .fetch(
            (jobId: 'brief-fixture', scope: BriefAudienceScope.issued),
            linkId: 'link',
            revisionId: 'revision',
            after: 'last',
          );
      expect(brief.issue, 'Shared feeder fault');
      expect(brief.audience!.issued.sequence, 1);
      expect(brief.audience!.current!.sequence, 2);
    },
  );

  testWidgets(
    'brief shows tasks, requirements, scope changes and asset navigation',
    (tester) async {
      Uri? launched;
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            jobBriefProvider.overrideWith(
              (ref, request) async => JobBrief.fromJson(fixture()),
            ),
            uriLauncherProvider.overrideWithValue((uri) async {
              launched = uri;
              return true;
            }),
          ],
          child: const MaterialApp(
            home: Scaffold(
              body: SingleChildScrollView(
                child: JobBriefPanel(jobId: 'brief-fixture'),
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('Shared feeder fault'), findsOneWidget);
      expect(find.text('Replace damaged section'), findsOneWidget);
      expect(find.text('Customer sign-off required'), findsOneWidget);
      expect(find.text('2 potentially affected subscriptions'), findsOneWidget);
      expect(
        find.text('The outage scope has changed since this job was issued.'),
        findsOneWidget,
      );
      expect(find.text('Customer One'), findsOneWidget);
      await tester.ensureVisible(find.text('Navigate'));
      await tester.tap(find.text('Navigate'));
      await tester.pump();
      expect(launched!.queryParameters['destination'], '9.1,7.4');
    },
  );

  testWidgets(
    'unknown audience stays unknown and absent coordinates have no navigation',
    (tester) async {
      final json = fixture();
      json['affected_subscription_count'] = null;
      json['affected_customers'] = [];
      json['directions'] = [
        {
          'label': 'Unlocated device',
          'coordinate_provenance': 'native_plant_record',
          'latitude': null,
          'longitude': null,
          'address_text': null,
        },
      ];
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            jobBriefProvider.overrideWith(
              (ref, request) async => JobBrief.fromJson(json),
            ),
          ],
          child: const MaterialApp(
            home: Scaffold(
              body: SingleChildScrollView(
                child: JobBriefPanel(jobId: 'brief-fixture'),
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('Affected audience unknown'), findsOneWidget);
      expect(find.text('0 potentially affected subscriptions'), findsNothing);
      expect(find.text('Location unavailable'), findsOneWidget);
      expect(find.text('Navigate'), findsNothing);
    },
  );

  testWidgets(
    'server refusal has a retry state and never shows an empty audience',
    (tester) async {
      var calls = 0;
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            jobBriefProvider.overrideWith((ref, request) async {
              if (calls++ == 0) throw StateError('job no longer assigned');
              return JobBrief.fromJson(fixture());
            }),
          ],
          child: const MaterialApp(
            home: Scaffold(
              body: SingleChildScrollView(
                child: JobBriefPanel(jobId: 'brief-fixture'),
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('Could not load the job brief'), findsOneWidget);
      expect(find.text('0 potentially affected subscriptions'), findsNothing);
      await tester.tap(find.text('Retry'));
      await tester.pumpAndSettle();
      expect(find.text('Shared feeder fault'), findsOneWidget);
    },
  );
}
