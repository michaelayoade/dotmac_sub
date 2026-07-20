import '../jobs/job_models.dart';

/// Pure projection of the server-owned completion contract plus local inputs.
class CompletionState {
  const CompletionState({
    this.requirements = JobCompletionRequirements.safeFallback,
    this.checklistDone = false,
    this.photoCount = 0,
    this.hasSignature = false,
    this.signerName,
    this.signatureUnavailableReason,
    this.equipmentSerial,
    this.summary,
  });

  final JobCompletionRequirements requirements;
  final bool checklistDone;
  final int photoCount;
  final bool hasSignature;
  final String? signerName;
  final String? signatureUnavailableReason;
  final String? equipmentSerial;
  final String? summary;

  bool get hasRequiredPhotos => photoCount >= requirements.minimumPhotoCount;

  bool get hasSignOff =>
      !requirements.customerSignoffRequired ||
      hasSignature ||
      (requirements.signatureUnavailableReasonAllowed &&
          signatureUnavailableReason != null &&
          signatureUnavailableReason!.trim().isNotEmpty);

  /// The server does not gate on the app's advisory quality checklist.
  bool get canComplete => hasRequiredPhotos && hasSignOff;

  /// Server-owned blockers shown under the disabled button.
  List<String> get blockers => [
    if (!hasRequiredPhotos)
      requirements.minimumPhotoCount == 1
          ? 'Add at least one photo'
          : 'Add at least ${requirements.minimumPhotoCount} photos',
    if (!hasSignOff)
      requirements.signatureUnavailableReasonAllowed
          ? 'Capture a signature (or note why unavailable)'
          : 'Capture a customer signature',
  ];

  Map<String, dynamic> get transitionPayload => {
    if (signatureUnavailableReason != null &&
        signatureUnavailableReason!.trim().isNotEmpty)
      'signature_unavailable_reason': signatureUnavailableReason!.trim(),
    if (signerName != null && signerName!.trim().isNotEmpty)
      'signer_name': signerName!.trim(),
    if (summary != null && summary!.trim().isNotEmpty)
      'summary': summary!.trim(),
  };

  CompletionState copyWith({
    bool? checklistDone,
    int? photoCount,
    bool? hasSignature,
    String? signerName,
    String? signatureUnavailableReason,
    String? equipmentSerial,
    String? summary,
  }) => CompletionState(
    requirements: requirements,
    checklistDone: checklistDone ?? this.checklistDone,
    photoCount: photoCount ?? this.photoCount,
    hasSignature: hasSignature ?? this.hasSignature,
    signerName: signerName ?? this.signerName,
    signatureUnavailableReason:
        signatureUnavailableReason ?? this.signatureUnavailableReason,
    equipmentSerial: equipmentSerial ?? this.equipmentSerial,
    summary: summary ?? this.summary,
  );
}
