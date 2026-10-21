/// Complete private attachment metadata returned by the scoped Field API.
class JobAttachment {
  const JobAttachment({
    required this.id,
    required this.workOrderId,
    required this.kind,
    required this.fileName,
    required this.mimeType,
    required this.sizeBytes,
    required this.createdAt,
    required this.downloadPath,
    this.noteId,
    this.latitude,
    this.longitude,
    this.capturedAt,
    this.signerName,
    this.uploadedByPersonId,
    this.uploadedBySystemUserId,
    this.clientRef,
    this.assetType,
    this.assetId,
  });
  final String id;
  final String workOrderId;
  final String? noteId;
  final String kind;
  final String fileName;
  final String mimeType;
  final int sizeBytes;
  final double? latitude;
  final double? longitude;
  final DateTime? capturedAt;
  final String? signerName;
  final String? uploadedByPersonId;
  final String? uploadedBySystemUserId;
  final String? clientRef;
  final String? assetType;
  final String? assetId;
  final DateTime createdAt;
  final String downloadPath;
  factory JobAttachment.fromJson(Map<String, dynamic> json) => JobAttachment(
    id: json['id'] as String,
    workOrderId: json['work_order_id'] as String,
    noteId: json['note_id'] as String?,
    kind: json['kind'] as String,
    fileName: json['file_name'] as String,
    mimeType: json['mime_type'] as String,
    sizeBytes: json['size_bytes'] as int,
    latitude: (json['latitude'] as num?)?.toDouble(),
    longitude: (json['longitude'] as num?)?.toDouble(),
    capturedAt: json['captured_at'] == null
        ? null
        : DateTime.parse(json['captured_at'] as String),
    signerName: json['signer_name'] as String?,
    uploadedByPersonId: json['uploaded_by_person_id'] as String?,
    uploadedBySystemUserId: json['uploaded_by_system_user_id'] as String?,
    clientRef: json['client_ref'] as String?,
    assetType: json['asset_type'] as String?,
    assetId: json['asset_id'] as String?,
    createdAt: DateTime.parse(json['created_at'] as String),
    downloadPath: json['download_path'] as String,
  );
}
