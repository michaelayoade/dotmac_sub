import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/offline/draft_store.dart';
import 'completion_review_models.dart';

/// Local evidence only. Restoring this record never refreshes tokens or sends it.
class CompletionEvidenceDraft {
  const CompletionEvidenceDraft({
    required this.intent,
    required this.attempted,
    required this.refused,
  });
  final CompletionEvidenceIntent intent;
  final bool attempted;
  final bool refused;

  Map<String, dynamic> toJson() {
    final preview = intent.preview;
    return {
      'version': 2,
      'intent_id': intent.intentId,
      'reason': intent.reason,
      'attempted': attempted,
      'refused': refused,
      'preview': {
        'enrollment_id': preview.enrollmentId,
        'criteria_revision_id': preview.criteriaRevisionId,
        'completed_event_id': preview.completedEventId,
        'provenance': preview.provenance?.value,
        'engineering_issuance_id': preview.engineeringIssuanceId,
        'criteria_generation': preview.criteriaGeneration,
        'work_order_id': preview.workOrderId,
        'assignment_generation': preview.assignmentGeneration,
        'generation': preview.generation,
        'policy_revision_id': preview.policyRevisionId,
        'review_state': preview.state.value,
        'submission_available': preview.submissionAvailable,
        'submission_unavailable_reason': preview.submissionUnavailableReason,
        'criteria': [
          for (final criterion in preview.criteria)
            {
              'id': criterion.id,
              'order': criterion.order,
              'statement': criterion.statement,
              'verification_method': criterion.verificationMethod,
            },
        ],
      },
      'observations': intent.observations
          .map((item) => item.toJson())
          .toList(growable: false),
    };
  }

  factory CompletionEvidenceDraft.fromJson(Map<String, dynamic> json) {
    if (json['version'] != 2) {
      throw const FormatException('Unknown completion draft version');
    }
    final preview = CompletionReviewPreview.fromJson(
      (json['preview'] as Map).cast<String, dynamic>(),
    );
    final observations = (json['observations'] as List)
        .map((raw) {
          final item = (raw as Map).cast<String, dynamic>();
          return CompletionCriterionObservation(
            criterionId: item['criterion_id'] as String,
            observation: item['observation'] as String,
            attachmentIds: List<String>.unmodifiable(
              (item['attachment_ids'] as List).cast<String>(),
            ),
          );
        })
        .toList(growable: false);
    if (observations.length != preview.criteria.length ||
        observations.map((item) => item.criterionId).toSet().length !=
            observations.length ||
        !observations.every(
          (item) => preview.criteria.any(
            (criterion) => criterion.id == item.criterionId,
          ),
        )) {
      throw const FormatException(
        'Completion draft criteria do not match its enrollment',
      );
    }
    return CompletionEvidenceDraft(
      intent: CompletionEvidenceIntent(
        intentId: json['intent_id'] as String,
        reason: json['reason'] as String,
        preview: preview,
        observations: observations,
      ),
      attempted: json['attempted'] as bool,
      refused: json['refused'] as bool,
    );
  }
}

/// Uses the existing AES-GCM draft rows and principal/deployment work gate.
/// No files, keys, schema, sync routes or automatic delivery are added.
class CompletionReviewDraftStore {
  const CompletionReviewDraftStore(this.store);
  final DraftStore store;
  String _id(String jobId) => 'completion_review:$jobId';
  Future<CompletionEvidenceDraft?> load(String jobId) async {
    final json = await store.load(_id(jobId));
    if (json == null) return null;
    if (json['job_id'] != jobId) {
      throw const FormatException('Completion draft job mismatch');
    }
    return CompletionEvidenceDraft.fromJson(json);
  }

  Future<void> save(String jobId, CompletionEvidenceDraft draft) => store.save(
    id: _id(jobId),
    type: 'completion_review',
    payload: {'job_id': jobId, ...draft.toJson()},
  );
  Future<void> delete(String jobId) => store.delete(_id(jobId));
}

final completionReviewDraftStoreProvider = Provider<CompletionReviewDraftStore>(
  (ref) => CompletionReviewDraftStore(ref.watch(draftStoreProvider)),
);
final completionReviewDraftProvider = FutureProvider.autoDispose
    .family<CompletionEvidenceDraft?, String>(
      (ref, jobId) => ref.watch(completionReviewDraftStoreProvider).load(jobId),
    );
