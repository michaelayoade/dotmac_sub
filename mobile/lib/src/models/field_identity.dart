/// Customer-safe reviewed identity. No subject, KYC, file key or public URL.
enum FieldIdentityUnavailable {
  unassigned,
  crewNotSelected,
  notApproved,
  unavailable,
}

class ApprovedFieldIdentity {
  const ApprovedFieldIdentity({
    required this.displayName,
    required this.companyName,
    required this.reviewedAt,
    required this.portraitRevision,
    required this.assignmentGeneration,
  });
  final String displayName;
  final String companyName;
  final DateTime reviewedAt;
  final int portraitRevision;
  final String assignmentGeneration;
  bool sameRevision(ApprovedFieldIdentity other) =>
      assignmentGeneration == other.assignmentGeneration &&
      portraitRevision == other.portraitRevision &&
      displayName == other.displayName &&
      companyName == other.companyName &&
      reviewedAt == other.reviewedAt;
}

class FieldIdentityResult {
  const FieldIdentityResult({
    required this.workOrderId,
    this.approved,
    this.unavailable = FieldIdentityUnavailable.unavailable,
  });
  final String workOrderId;
  final ApprovedFieldIdentity? approved;
  final FieldIdentityUnavailable unavailable;

  factory FieldIdentityResult.fromJson(
    Map<String, dynamic> json, {
    required String expectedWorkOrderId,
  }) {
    if (json['work_order_id'] != expectedWorkOrderId) {
      throw const FormatException('Identity visit mismatch');
    }
    final raw = json['card'];
    if (raw is Map &&
        raw['verification_state'] == 'reviewed' &&
        json['unavailable_reason'] == null) {
      final name = raw['display_name'];
      final company = raw['company_name'];
      final revision = raw['portrait_revision'];
      final generation = raw['assignment_generation'];
      final reviewed = DateTime.tryParse(raw['reviewed_at']?.toString() ?? '');
      if (name is! String ||
          name.trim().isEmpty ||
          company is! String ||
          company.trim().isEmpty ||
          revision is! int ||
          revision < 1 ||
          generation is! String ||
          !isNativeIdentityId(generation) ||
          reviewed == null) {
        throw const FormatException('Invalid reviewed identity');
      }
      return FieldIdentityResult(
        workOrderId: expectedWorkOrderId,
        approved: ApprovedFieldIdentity(
          displayName: name,
          companyName: company,
          reviewedAt: reviewed,
          portraitRevision: revision,
          assignmentGeneration: generation,
        ),
      );
    }
    final reason = switch (json['unavailable_reason']) {
      'actual_crew_not_selected' => FieldIdentityUnavailable.crewNotSelected,
      'unassigned' => FieldIdentityUnavailable.unassigned,
      'identity_not_approved' => FieldIdentityUnavailable.notApproved,
      _ => FieldIdentityUnavailable.unavailable,
    };
    return FieldIdentityResult(
      workOrderId: expectedWorkOrderId,
      unavailable: reason,
    );
  }
}

bool isNativeIdentityId(String id) => RegExp(
      r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$',
    ).hasMatch(id);
