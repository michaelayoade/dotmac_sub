/// Immutable enrolled criteria and review facts supplied by the completion owner.
enum CompletionReviewState {
  legacyNotReviewed('legacy_not_reviewed', 'Not reviewed'),
  submitted('submitted', 'Awaiting supervisor review'),
  accepted('accepted', 'Last staff review: accepted'),
  rejected('rejected', 'Last staff review: rejected'),
  revoked('revoked', 'Last staff review: revoked'),
  stale('stale', 'Review requires refresh');

  const CompletionReviewState(this.value, this.label);
  final String value;
  final String label;
  static CompletionReviewState parse(String value) =>
      values.firstWhere((state) => state.value == value);
}

class IssuedCompletionCriterion {
  const IssuedCompletionCriterion({
    required this.id,
    required this.order,
    required this.statement,
    required this.verificationMethod,
  });
  final String id;
  final int order;
  final String statement;
  final String verificationMethod;
  factory IssuedCompletionCriterion.fromJson(Map<String, dynamic> json) =>
      IssuedCompletionCriterion(
        id: json['id'] as String,
        order: json['order'] as int,
        statement: json['statement'] as String,
        verificationMethod: json['verification_method'] as String,
      );
}

enum CompletionProvenance {
  engineeringIssuance('engineering_issuance', 'Engineering issuance'),
  nativeWorkOrder('native_work_order', 'Native work order');

  const CompletionProvenance(this.value, this.label);
  final String value;
  final String label;
  static CompletionProvenance parse(String value) =>
      values.firstWhere((item) => item.value == value);
}

class CompletionReviewPreview {
  const CompletionReviewPreview({
    required this.enrollmentId,
    required this.criteriaRevisionId,
    required this.completedEventId,
    required this.provenance,
    required this.engineeringIssuanceId,
    required this.workOrderId,
    required this.assignmentGeneration,
    required this.generation,
    required this.criteriaGeneration,
    required this.policyRevisionId,
    required this.criteria,
    required this.state,
    required this.submissionAvailable,
    this.submissionUnavailableReason,
  });
  final String? enrollmentId;
  final String? criteriaRevisionId;
  final String? completedEventId;
  final CompletionProvenance? provenance;
  final String? engineeringIssuanceId;
  final String workOrderId;
  final String? assignmentGeneration;
  final int generation;
  final int criteriaGeneration;
  final String? policyRevisionId;
  final List<IssuedCompletionCriterion> criteria;
  final CompletionReviewState state;
  final bool submissionAvailable;
  final String? submissionUnavailableReason;
  factory CompletionReviewPreview.fromJson(Map<String, dynamic> json) =>
      CompletionReviewPreview(
        enrollmentId: json['enrollment_id'] as String?,
        criteriaRevisionId: json['criteria_revision_id'] as String?,
        completedEventId: json['completed_event_id'] as String?,
        provenance: json['provenance'] == null
            ? null
            : CompletionProvenance.parse(json['provenance'] as String),
        engineeringIssuanceId: json['engineering_issuance_id'] as String?,
        workOrderId: json['work_order_id'] as String,
        assignmentGeneration: json['assignment_generation'] as String?,
        generation: json['generation'] as int,
        criteriaGeneration: json['criteria_generation'] as int,
        policyRevisionId: json['policy_revision_id'] as String?,
        criteria: (json['criteria'] as List)
            .map(
              (item) => IssuedCompletionCriterion.fromJson(
                (item as Map).cast<String, dynamic>(),
              ),
            )
            .toList(growable: false),
        state: CompletionReviewState.parse(json['review_state'] as String),
        submissionAvailable: json['submission_available'] as bool,
        submissionUnavailableReason:
            json['submission_unavailable_reason'] as String?,
      );
}

class CompletionCriterionObservation {
  const CompletionCriterionObservation({
    required this.criterionId,
    required this.observation,
    this.attachmentIds = const [],
  });
  final String criterionId;
  final String observation;
  final List<String> attachmentIds;
  Map<String, Object> toJson() => {
    'criterion_id': criterionId,
    'observation': observation,
    'attachment_ids': attachmentIds,
  };
}

class CompletionEvidenceIntent {
  const CompletionEvidenceIntent({
    required this.intentId,
    required this.reason,
    required this.preview,
    required this.observations,
  });
  final String intentId;
  final String reason;
  final CompletionReviewPreview preview;
  final List<CompletionCriterionObservation> observations;
  Map<String, Object?> toJson() => {
    'intent_id': intentId,
    'reason': reason,
    'confirmed': true,
    'enrollment_id': preview.enrollmentId,
    'criteria_revision_id': preview.criteriaRevisionId,
    'completed_event_id': preview.completedEventId,
    'assignment_generation': preview.assignmentGeneration,
    'expected_generation': preview.generation,
    'policy_revision_id': preview.policyRevisionId,
    'observations': observations
        .map((item) => item.toJson())
        .toList(growable: false),
  };
}

class CompletionEvidenceReceipt {
  const CompletionEvidenceReceipt({
    required this.enrollmentId,
    required this.criteriaRevisionId,
    required this.completedEventId,
    required this.provenance,
    required this.engineeringIssuanceId,
    required this.policyRevisionId,
    required this.submissionId,
    required this.decisionId,
    required this.generation,
    required this.criteriaGeneration,
    required this.state,
    required this.replayed,
  });
  final String? enrollmentId;
  final String? criteriaRevisionId;
  final String? completedEventId;
  final CompletionProvenance? provenance;
  final String? engineeringIssuanceId;
  final String? policyRevisionId;
  final String? submissionId;
  final String? decisionId;
  final int generation;
  final int criteriaGeneration;
  final CompletionReviewState state;
  final bool replayed;
  factory CompletionEvidenceReceipt.fromJson(Map<String, dynamic> json) =>
      CompletionEvidenceReceipt(
        enrollmentId: json['enrollment_id'] as String?,
        criteriaRevisionId: json['criteria_revision_id'] as String?,
        completedEventId: json['completed_event_id'] as String?,
        provenance: json['provenance'] == null
            ? null
            : CompletionProvenance.parse(json['provenance'] as String),
        engineeringIssuanceId: json['engineering_issuance_id'] as String?,
        policyRevisionId: json['policy_revision_id'] as String?,
        submissionId: json['submission_id'] as String?,
        decisionId: json['decision_id'] as String?,
        generation: json['generation'] as int,
        criteriaGeneration: json['criteria_generation'] as int,
        state: CompletionReviewState.parse(json['state'] as String),
        replayed: json['replayed'] as bool,
      );
}
