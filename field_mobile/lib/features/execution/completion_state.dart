/// Pure gating model for the completion wizard — mirrors the server's
/// completion gate (>=1 photo, signature or documented fallback) so techs
/// get instant feedback instead of a server 422.
class CompletionState {
  const CompletionState({
    this.checklistDone = false,
    this.photoCount = 0,
    this.hasSignature = false,
    this.signerName,
    this.signatureUnavailableReason,
    this.equipmentSerial,
    this.summary,
  });

  final bool checklistDone;
  final int photoCount;
  final bool hasSignature;
  final String? signerName;
  final String? signatureUnavailableReason;
  final String? equipmentSerial;
  final String? summary;

  bool get hasPhoto => photoCount > 0;

  bool get hasSignOff =>
      hasSignature ||
      (signatureUnavailableReason != null &&
          signatureUnavailableReason!.trim().isNotEmpty);

  bool get canComplete => checklistDone && hasPhoto && hasSignOff;

  /// What still blocks completion — shown under the disabled button.
  List<String> get blockers => [
    if (!checklistDone) 'Finish the checklist',
    if (!hasPhoto) 'Add at least one photo',
    if (!hasSignOff) 'Capture a signature (or note why unavailable)',
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
