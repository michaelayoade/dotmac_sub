import 'dart:convert';
import 'package:dio/dio.dart';
import 'package:dotmac_field/core/offline/draft_store.dart';
import 'package:dotmac_field/features/execution/completion_review_drafts.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/features/execution/completion_review_models.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/execution/execution_controller.dart';
import 'package:dotmac_field/core/location/location_source.dart';
import 'package:dotmac_field/features/execution/completion_review_repository.dart';
import 'package:dotmac_field/features/execution/completion_review_screen.dart';
import 'package:dotmac_field/features/jobs/job_attachment.dart';
import 'package:dotmac_field/features/jobs/jobs_providers.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fake_http.dart';
import 'helpers/secure_store.dart';
import 'package:dotmac_field/core/secure/offline_wipe.dart';
import 'package:dotmac_field/core/secure/store_work_gate.dart';

Map<String, dynamic> previewJson() => {
  'enrollment_id': 'enrollment-1',
  'criteria_revision_id': 'criteria-revision-1',
  'completed_event_id': 'completed-event-1',
  'criteria_generation': 1,
  'provenance': 'native_work_order',
  'engineering_issuance_id': null,
  'work_order_id': 'WO-1',
  'assignment_generation': 'assignment-1',
  'generation': 3,
  'policy_revision_id': 'policy-1',
  'review_state': 'legacy_not_reviewed',
  'submission_available': true,
  'submission_unavailable_reason': null,
  'criteria': [
    {
      'id': 'criterion-1',
      'order': 0,
      'statement': 'Issued requirement',
      'verification_method': 'Record the specified reading',
    },
  ],
};

class FakeCompletionRepository extends CompletionReviewRepository {
  FakeCompletionRepository()
    : super(
        ApiClient(
          baseUrl: 'https://test.local',
          tokenStore: InMemoryTokenStore(),
        ),
      );
  final submitted = <CompletionEvidenceIntent>[];
  Object? failure;
  int reads = 0;
  @override
  Future<CompletionReviewPreview> preview(String jobId) async {
    reads++;
    return CompletionReviewPreview.fromJson({
      ...previewJson(),
      'generation': 4,
    });
  }

  @override
  Future<CompletionEvidenceReceipt> submit(
    String jobId,
    CompletionEvidenceIntent intent,
  ) async {
    submitted.add(intent);
    if (failure != null) throw failure!;
    return const CompletionEvidenceReceipt(
      enrollmentId: 'enrollment-1',
      criteriaRevisionId: 'criteria-revision-1',
      completedEventId: 'completed-event-1',
      provenance: CompletionProvenance.nativeWorkOrder,
      engineeringIssuanceId: null,
      policyRevisionId: 'policy-1',
      submissionId: 'submission-1',
      decisionId: null,
      generation: 4,
      criteriaGeneration: 1,
      state: CompletionReviewState.submitted,
      replayed: false,
    );
  }
}

class MemoryCompletionDraftStore extends CompletionReviewDraftStore {
  MemoryCompletionDraftStore() : super(const DraftStore());
  Map<String, dynamic>? saved;
  Object? saveFailure;
  @override
  Future<CompletionEvidenceDraft?> load(String jobId) async =>
      saved == null ? null : CompletionEvidenceDraft.fromJson(saved!);
  @override
  Future<void> save(String jobId, CompletionEvidenceDraft draft) async {
    if (saveFailure != null) throw saveFailure!;
    saved = (jsonDecode(jsonEncode(draft.toJson())) as Map)
        .cast<String, dynamic>();
  }

  @override
  Future<void> delete(String jobId) async {
    saved = null;
  }
}

Future<void> mountForm(
  WidgetTester tester,
  FakeCompletionRepository repository,
  MemoryCompletionDraftStore drafts, {
  CompletionReviewPreview? preview,
}) async {
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        completionReviewRepositoryProvider.overrideWithValue(repository),
        completionReviewDraftStoreProvider.overrideWithValue(drafts),
        jobDetailProvider.overrideWith(
          (ref, id) => Future.error(StateError('unavailable')),
        ),
      ],
      child: MaterialApp(
        home: CompletionReviewScreen(
          jobId: 'WO-1',
          preview: preview ?? CompletionReviewPreview.fromJson(previewJson()),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> openForm(
  WidgetTester tester,
  FakeCompletionRepository repository, {
  MemoryCompletionDraftStore? drafts,
}) async {
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        completionReviewRepositoryProvider.overrideWithValue(repository),
        if (drafts != null)
          completionReviewDraftStoreProvider.overrideWithValue(drafts),
        jobDetailProvider.overrideWith(
          (ref, id) => Future.error(StateError('unavailable')),
        ),
      ],
      child: MaterialApp(
        home: CompletionReviewScreen(
          jobId: 'WO-1',
          preview: CompletionReviewPreview.fromJson(previewJson()),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  await tester.enterText(
    find.byKey(const Key('criterion-observation-criterion-1')),
    'Observed reading as issued',
  );
  await tester.ensureVisible(
    find.widgetWithText(TextField, 'Submission reason'),
  );
  await tester.enterText(
    find.widgetWithText(TextField, 'Submission reason'),
    'Ready for review',
  );
  await tester.pumpAndSettle();
  await tester.ensureVisible(
    find.byKey(const Key('submit-completion-evidence')),
  );
  await tester.tap(find.byKey(const Key('submit-completion-evidence')));
  await tester.pumpAndSettle();
}

class FixedAuthController extends AuthController {
  FixedAuthController(this.initial);
  final AuthState initial;
  @override
  AuthState build() => initial;
}

void main() {
  test('vendor and unresolved sessions never construct a GPS source', () async {
    for (final auth in <AuthState>[
      const Authenticated(LoginMode.vendor),
      const Unauthenticated(),
    ]) {
      final container = ProviderContainer(
        overrides: [
          authControllerProvider.overrideWith(() => FixedAuthController(auth)),
        ],
      );
      try {
        final source = container.read(locationSourceProvider);
        expect(source, isA<UnavailableLocation>());
        expect(await source.current(), isNull);
      } finally {
        container.dispose();
      }
    }
  });
  test(
    'typed DTO preserves issued identity, generation, observation and attachment IDs',
    () {
      final preview = CompletionReviewPreview.fromJson(previewJson());
      final intent = CompletionEvidenceIntent(
        intentId: 'intent-1',
        reason: 'Ready',
        preview: preview,
        observations: const [
          CompletionCriterionObservation(
            criterionId: 'criterion-1',
            observation: 'Reading',
            attachmentIds: ['attachment-1'],
          ),
        ],
      );
      expect(intent.toJson(), {
        'intent_id': 'intent-1',
        'reason': 'Ready',
        'confirmed': true,
        'enrollment_id': 'enrollment-1',
        'criteria_revision_id': 'criteria-revision-1',
        'completed_event_id': 'completed-event-1',
        'assignment_generation': 'assignment-1',
        'expected_generation': 3,
        'policy_revision_id': 'policy-1',
        'observations': [
          {
            'criterion_id': 'criterion-1',
            'observation': 'Reading',
            'attachment_ids': ['attachment-1'],
          },
        ],
      });
      expect(
        preview.criteria.single.verificationMethod,
        'Record the specified reading',
      );
      expect(preview.state, CompletionReviewState.legacyNotReviewed);
    },
  );

  test(
    'private attachment DTO retains identity instead of using download path as evidence ID',
    () {
      final attachment = JobAttachment.fromJson({
        'id': 'attachment-1',
        'work_order_id': 'WO-1',
        'kind': 'photo',
        'file_name': 'reading.jpg',
        'mime_type': 'image/jpeg',
        'size_bytes': 512,
        'created_at': '2026-10-11T12:00:00Z',
        'download_path': '/api/v1/field/attachments/attachment-1/content',
      });
      expect(attachment.id, 'attachment-1');
      expect(attachment.fileName, 'reading.jpg');
      expect(attachment.downloadPath, contains('/content'));
    },
  );

  test(
    'scoped repository exposes structured refusal without retrying or refreshing tokens',
    () async {
      final adapter = FakeHttpAdapter();
      adapter.on(
        'GET',
        '/api/v1/field/jobs/WO-1/completion-review',
        (_) => (
          409,
          {
            'detail': {
              'code': 'completion.stale',
              'message': 'Assignment changed',
            },
          },
        ),
      );
      final dio = Dio()..httpClientAdapter = adapter;
      final api = ApiClient(
        baseUrl: 'https://test.local',
        tokenStore: InMemoryTokenStore(),
        dio: dio,
      );
      await expectLater(
        CompletionReviewRepository(api).preview('WO-1'),
        throwsA(
          isA<CompletionReviewRefusal>()
              .having((e) => e.code, 'code', 'completion.stale')
              .having((e) => e.message, 'message', 'Assignment changed'),
        ),
      );
      expect(adapter.requests, hasLength(1));
    },
  );

  test(
    'completion drafts use encrypted scoped rows and the existing sign-out wipe',
    () async {
      final device = newTestDevice();
      final one = await device.open(techOne);
      final two = await device.open(techTwo);
      final otherDeployment = await device.open(techOneOtherTenant);
      final backing = DraftStore(
        db: one.database,
        cipher: one.cipher,
        scopeKey: one.scopeKey,
      );
      final drafts = CompletionReviewDraftStore(backing);
      final draft = CompletionEvidenceDraft(
        intent: CompletionEvidenceIntent(
          intentId: 'stable-draft-intent',
          reason: 'Private site evidence reason',
          preview: CompletionReviewPreview.fromJson(previewJson()),
          observations: const [
            CompletionCriterionObservation(
              criterionId: 'criterion-1',
              observation: 'Private measured result',
              attachmentIds: ['attachment-1'],
            ),
          ],
        ),
        attempted: true,
        refused: true,
      );
      await drafts.save('WO-1', draft);
      final row = await one.database
          .select(one.database.draftEntries)
          .getSingle();
      expect(row.payloadJson, isNot(contains('Private measured result')));
      expect(row.payloadJson, isNot(contains('Private site evidence reason')));
      final restarted = CompletionReviewDraftStore(
        DraftStore(
          db: one.database,
          cipher: one.cipher,
          scopeKey: one.scopeKey,
        ),
      );
      expect((await restarted.load('WO-1'))!.toJson(), draft.toJson());
      expect(await restarted.load('WO-2'), isNull);
      for (final other in [two, otherDeployment]) {
        final isolated = CompletionReviewDraftStore(
          DraftStore(
            db: one.database,
            cipher: other.cipher,
            scopeKey: other.scopeKey,
          ),
        );
        expect(await isolated.load('WO-1'), isNull);
      }
      await device.wipe.wipe(
        WipeRequest(
          scopeKey: one.scopeKey,
          trigger: WipeTrigger.explicitLogout,
        ),
        live: one,
      );
      await expectLater(
        drafts.save('WO-1', draft),
        throwsA(isA<StoreDiscarded>()),
      );
      final fresh = await device.open(techOne);
      expect(
        await CompletionReviewDraftStore(
          DraftStore(
            db: fresh.database,
            cipher: fresh.cipher,
            scopeKey: fresh.scopeKey,
          ),
        ).load('WO-1'),
        isNull,
      );
    },
  );

  testWidgets(
    'unenrolled native job renders owner readiness without an engineering issuance dead end',
    (tester) async {
      final repository = FakeCompletionRepository();
      final drafts = MemoryCompletionDraftStore();
      final preview = CompletionReviewPreview.fromJson({
        ...previewJson(),
        'enrollment_id': null,
        'criteria_revision_id': null,
        'completed_event_id': null,
        'criteria_generation': 0,
        'provenance': 'native_work_order',
        'engineering_issuance_id': null,
        'criteria': [],
        'submission_available': false,
        'submission_unavailable_reason':
            'Ask your supervisor to enroll completion review',
      });
      await mountForm(tester, repository, drafts, preview: preview);
      expect(
        find.text('Ask your supervisor to enroll completion review'),
        findsOneWidget,
      );
      expect(
        find.text('Completion review is not configured for this job.'),
        findsOneWidget,
      );
      expect(repository.submitted, isEmpty);
      expect(
        (drafts.saved!['preview'] as Map)['engineering_issuance_id'],
        isNull,
      );
    },
  );

  testWidgets(
    'older issuance drafts remain retained and blocked until an explicit reload',
    (tester) async {
      final repository = FakeCompletionRepository();
      final drafts = MemoryCompletionDraftStore()
        ..saved = {'version': 1, 'legacy': 'Original evidence'};
      await mountForm(tester, repository, drafts);
      expect(drafts.saved!['legacy'], 'Original evidence');
      expect(repository.submitted, isEmpty);
      await tester.scrollUntilVisible(
        find.byKey(const Key('submit-completion-evidence')),
        180,
        scrollable: find.byType(Scrollable).first,
      );
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const Key('submit-completion-evidence')),
            )
            .onPressed,
        isNull,
      );
      await tester.scrollUntilVisible(
        find.byKey(const Key('reload-completion-review')),
        180,
        scrollable: find.byType(Scrollable).first,
      );
      await tester.tap(find.byKey(const Key('reload-completion-review')));
      await tester.pumpAndSettle();
      expect(drafts.saved!['version'], 2);
      expect(drafts.saved!.containsKey('legacy'), isFalse);
      expect(repository.reads, 1);
      expect(repository.submitted, isEmpty);
    },
  );

  testWidgets(
    'closing and restoring an attempted draft keeps original tokens and sends nothing automatically',
    (tester) async {
      final repository = FakeCompletionRepository()
        ..failure = StateError('transport');
      final drafts = MemoryCompletionDraftStore();
      await openForm(tester, repository, drafts: drafts);
      final original = repository.submitted.single;
      expect(drafts.saved!['attempted'], isTrue);
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pumpAndSettle();
      await mountForm(
        tester,
        repository,
        drafts,
        preview: CompletionReviewPreview.fromJson({
          ...previewJson(),
          'generation': 99,
        }),
      );
      expect(repository.submitted, hasLength(1));
      expect(repository.reads, 0);
      expect(find.textContaining('Review version: 3'), findsOneWidget);
      expect(
        tester
            .widget<TextField>(
              find.byKey(const Key('criterion-observation-criterion-1')),
            )
            .controller!
            .text,
        'Observed reading as issued',
      );
      repository.failure = null;
      await tester.scrollUntilVisible(
        find.byKey(const Key('submit-completion-evidence')),
        180,
        scrollable: find.byType(Scrollable).first,
      );
      await tester.tap(find.byKey(const Key('submit-completion-evidence')));
      await tester.pumpAndSettle();
      expect(repository.submitted.last.toJson(), original.toJson());
      expect(drafts.saved, isNull);
    },
  );

  testWidgets(
    'refused draft remains blocked after restore and reload explicitly replaces its intent',
    (tester) async {
      final repository = FakeCompletionRepository()
        ..failure = const CompletionReviewRefusal(
          code: 'stale',
          message: 'Review changed',
        );
      final drafts = MemoryCompletionDraftStore();
      await openForm(tester, repository, drafts: drafts);
      final oldIntent = repository.submitted.single.intentId;
      expect(drafts.saved!['refused'], isTrue);
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pumpAndSettle();
      await mountForm(tester, repository, drafts);
      expect(repository.submitted, hasLength(1));
      await tester.scrollUntilVisible(
        find.byKey(const Key('submit-completion-evidence')),
        180,
        scrollable: find.byType(Scrollable).first,
      );
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const Key('submit-completion-evidence')),
            )
            .onPressed,
        isNull,
      );
      await tester.scrollUntilVisible(
        find.byKey(const Key('reload-completion-review')),
        180,
        scrollable: find.byType(Scrollable).first,
      );
      await tester.tap(find.byKey(const Key('reload-completion-review')));
      await tester.pumpAndSettle();
      expect(drafts.saved!['intent_id'], isNot(oldIntent));
      expect((drafts.saved!['preview'] as Map)['generation'], 4);
      expect(drafts.saved!['attempted'], isFalse);
    },
  );

  testWidgets('a draft save failure prevents sending an unpersisted intent', (
    tester,
  ) async {
    final repository = FakeCompletionRepository();
    final drafts = MemoryCompletionDraftStore()
      ..saveFailure = StateError('storage unavailable');
    await openForm(tester, repository, drafts: drafts);
    expect(repository.submitted, isEmpty);
    expect(
      find.text(
        'Could not save this intent. Submission was not sent. Retry saving before sending.',
      ),
      findsOneWidget,
    );
  });

  testWidgets(
    'uncertain delivery freezes content and retries the original intent and tokens',
    (tester) async {
      final repository = FakeCompletionRepository()
        ..failure = StateError('transport');
      await openForm(tester, repository);
      expect(repository.submitted, hasLength(1));
      final original = repository.submitted.single;
      expect(
        find.text(
          'Delivery could not be confirmed. Retry sends the same intent and evidence.',
        ),
        findsOneWidget,
      );
      expect(
        tester
            .widget<TextField>(
              find.byKey(const Key('criterion-observation-criterion-1')),
            )
            .enabled,
        isFalse,
      );
      repository.failure = null;
      await tester.ensureVisible(
        find.byKey(const Key('submit-completion-evidence')),
      );
      await tester.tap(find.byKey(const Key('submit-completion-evidence')));
      await tester.pumpAndSettle();
      expect(identical(repository.submitted.last, original), isTrue);
      expect(original.preview.generation, 3);
      expect(repository.reads, 0);
      await tester.drag(find.byType(ListView), const Offset(0, 1600));
      await tester.pumpAndSettle();
      expect(find.text('Awaiting supervisor review'), findsOneWidget);
      expect(find.text('Last staff review: accepted'), findsNothing);
    },
  );

  testWidgets(
    'refusal blocks submission until explicit reload creates a new form',
    (tester) async {
      final repository = FakeCompletionRepository()
        ..failure = const CompletionReviewRefusal(
          code: 'completion.stale',
          message: 'Review changed',
        );
      await openForm(tester, repository);
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const Key('submit-completion-evidence')),
            )
            .onPressed,
        isNull,
      );
      expect(repository.reads, 0);
      await tester.scrollUntilVisible(
        find.byKey(const Key('reload-completion-review')),
        180,
        scrollable: find.byType(Scrollable).first,
      );
      await tester.tap(find.byKey(const Key('reload-completion-review')));
      await tester.pumpAndSettle();
      expect(repository.reads, 1);
      await tester.drag(find.byType(ListView), const Offset(0, 1600));
      await tester.pumpAndSettle();
      expect(find.textContaining('Review version: 4'), findsOneWidget);
      expect(
        tester
            .widget<TextField>(
              find.byKey(const Key('criterion-observation-criterion-1')),
            )
            .enabled,
        isTrue,
      );
      expect(repository.submitted, hasLength(1));
    },
  );
}
