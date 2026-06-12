/// Mirrors MyLocationRead / MyLocationRequestRead from app/schemas/gis.py.
class LocationRequest {
  LocationRequest({
    required this.id,
    required this.status,
    required this.latitude,
    required this.longitude,
    this.note,
    this.reviewNote,
    this.createdAt,
    this.reviewedAt,
  });

  final String id;
  final String status;
  final double latitude;
  final double longitude;
  final String? note;
  final String? reviewNote;
  final DateTime? createdAt;
  final DateTime? reviewedAt;

  factory LocationRequest.fromJson(Map<String, dynamic> json) =>
      LocationRequest(
        id: json['id'].toString(),
        status: json['status'] as String? ?? 'pending',
        latitude: (json['requested_latitude'] as num).toDouble(),
        longitude: (json['requested_longitude'] as num).toDouble(),
        note: json['customer_note'] as String?,
        reviewNote: json['review_note'] as String?,
        createdAt: json['created_at'] != null
            ? DateTime.tryParse(json['created_at'] as String)
            : null,
        reviewedAt: json['reviewed_at'] != null
            ? DateTime.tryParse(json['reviewed_at'] as String)
            : null,
      );
}

class ServiceLocation {
  ServiceLocation({
    required this.canSubmitRequest,
    required this.hasAddressAnchor,
    this.addressLabel,
    this.latitude,
    this.longitude,
    this.pendingRequest,
    this.history = const [],
  });

  final bool canSubmitRequest;
  final bool hasAddressAnchor;
  final String? addressLabel;
  final double? latitude;
  final double? longitude;
  final LocationRequest? pendingRequest;
  final List<LocationRequest> history;

  bool get hasPin => latitude != null && longitude != null;

  factory ServiceLocation.fromJson(Map<String, dynamic> json) =>
      ServiceLocation(
        canSubmitRequest: json['can_submit_request'] as bool? ?? false,
        hasAddressAnchor: json['has_address_anchor'] as bool? ?? false,
        addressLabel: json['address_label'] as String?,
        latitude: (json['current_latitude'] as num?)?.toDouble(),
        longitude: (json['current_longitude'] as num?)?.toDouble(),
        pendingRequest: json['pending_request'] != null
            ? LocationRequest.fromJson(
                json['pending_request'] as Map<String, dynamic>,
              )
            : null,
        history: [
          for (final item in (json['history'] as List? ?? const []))
            LocationRequest.fromJson(item as Map<String, dynamic>),
        ],
      );
}
