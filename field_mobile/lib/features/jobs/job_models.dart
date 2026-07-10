import '../../core/location/map_coordinates.dart';

/// Wire models for the field jobs API. Hand-rolled fromJson keeps the app
/// free of codegen for plain DTOs.
class JobSummary {
  const JobSummary({
    required this.id,
    required this.title,
    required this.status,
    required this.workType,
    required this.priority,
    this.description,
    this.scheduledStart,
    this.scheduledEnd,
    this.estimatedDurationMinutes,
    this.startedAt,
    this.pausedAt,
    this.resumedAt,
    this.completedAt,
    this.totalActiveSeconds,
  });

  final String id;
  final String title;
  final String status;
  final String workType;
  final String priority;
  final String? description;
  final DateTime? scheduledStart;
  final DateTime? scheduledEnd;
  final int? estimatedDurationMinutes;
  final DateTime? startedAt;
  final DateTime? pausedAt;
  final DateTime? resumedAt;
  final DateTime? completedAt;
  final int? totalActiveSeconds;

  factory JobSummary.fromJson(Map<String, dynamic> json) => JobSummary(
    id: json['id'] as String,
    title: json['title'] as String,
    status: json['status'] as String,
    workType: json['work_type'] as String,
    priority: json['priority'] as String,
    description: json['description'] as String?,
    scheduledStart: _date(json['scheduled_start']),
    scheduledEnd: _date(json['scheduled_end']),
    estimatedDurationMinutes: json['estimated_duration_minutes'] as int?,
    startedAt: _date(json['started_at']),
    pausedAt: _date(json['paused_at']),
    resumedAt: _date(json['resumed_at']),
    completedAt: _date(json['completed_at']),
    totalActiveSeconds: json['total_active_seconds'] as int?,
  );
}

class JobCustomer {
  const JobCustomer({
    this.name,
    this.phone,
    this.addressText,
    this.servicePlan,
    this.accountNumber,
  });

  final String? name;
  final String? phone;
  final String? addressText;
  final String? servicePlan;
  final String? accountNumber;

  factory JobCustomer.fromJson(Map<String, dynamic> json) => JobCustomer(
    name: json['name'] as String?,
    phone: json['phone'] as String?,
    addressText: json['address_text'] as String?,
    servicePlan: json['service_plan'] as String?,
    accountNumber: json['account_number'] as String?,
  );
}

class JobLocation {
  const JobLocation({
    this.latitude,
    this.longitude,
    this.addressText,
    required this.source,
  });

  final double? latitude;
  final double? longitude;
  final String? addressText;
  final String source;

  factory JobLocation.fromJson(Map<String, dynamic> json) => JobLocation(
    latitude: (json['latitude'] as num?)?.toDouble(),
    longitude: (json['longitude'] as num?)?.toDouble(),
    addressText: json['address_text'] as String?,
    source: json['source'] as String? ?? 'none',
  );

  Map<String, dynamic> toJson() => {
    'latitude': latitude,
    'longitude': longitude,
    'address_text': addressText,
    'source': source,
  };

  bool get hasCoordinates {
    return isValidMapCoordinate(latitude, longitude);
  }

  /// Navigation handoff: precise coordinates when geocoded, otherwise a
  /// text search the maps app can resolve. Null when nothing is known.
  Uri? get mapsUri {
    if (hasCoordinates) {
      return Uri.parse('geo:$latitude,$longitude?q=$latitude,$longitude');
    }
    final address = addressText;
    if (address == null || address.isEmpty) return null;
    return Uri.parse('geo:0,0?q=${Uri.encodeComponent(address)}');
  }
}

class JobDestination {
  const JobDestination({
    required this.destinationType,
    this.destinationId,
    required this.label,
    this.latitude,
    this.longitude,
    this.addressText,
  });

  final String destinationType;
  final String? destinationId;
  final String label;
  final double? latitude;
  final double? longitude;
  final String? addressText;

  factory JobDestination.fromJson(Map<String, dynamic> json) => JobDestination(
    destinationType: json['destination_type'] as String? ?? 'other',
    destinationId: json['destination_id']?.toString(),
    label: json['label'] as String? ?? 'Destination',
    latitude: (json['latitude'] as num?)?.toDouble(),
    longitude: (json['longitude'] as num?)?.toDouble(),
    addressText: json['address_text'] as String?,
  );

  Map<String, dynamic> toTransitionPayload() => {
    'destination_type': destinationType,
    'destination_id': ?destinationId,
    'destination_label': label,
    'destination_latitude': ?latitude,
    'destination_longitude': ?longitude,
  };
}

class JobDetail {
  const JobDetail({
    required this.job,
    required this.location,
    this.customer,
    this.ticketRef,
    this.notes = const [],
    this.materials = const [],
    this.materialRequests = const [],
    this.history = const [],
  });

  final JobSummary job;
  final JobLocation location;
  final JobCustomer? customer;
  final String? ticketRef;
  final List<Map<String, dynamic>> notes;
  final List<Map<String, dynamic>> materials;
  final List<Map<String, dynamic>> materialRequests;
  final List<Map<String, dynamic>> history;

  factory JobDetail.fromJson(Map<String, dynamic> json) => JobDetail(
    job: JobSummary.fromJson((json['job'] as Map).cast<String, dynamic>()),
    location: JobLocation.fromJson(
      (json['location'] as Map).cast<String, dynamic>(),
    ),
    customer: json['customer'] != null
        ? JobCustomer.fromJson(
            (json['customer'] as Map).cast<String, dynamic>(),
          )
        : null,
    ticketRef: json['ticket_ref'] as String?,
    notes: _mapList(json['notes']),
    materials: _mapList(json['materials']),
    materialRequests: _mapList(json['material_requests']),
    history: _mapList(json['history']),
  );
}

List<String> workActionsFor(String status) => switch (status) {
  'scheduled' => ['en_route', 'arrived', 'start'],
  'dispatched' => ['en_route', 'arrived', 'start'],
  'in_progress' => ['en_route', 'arrived', 'pause', 'complete'],
  'paused' => ['en_route', 'arrived', 'resume'],
  _ => const [],
};

String? primaryActionFor(String status) => switch (status) {
  'scheduled' => 'start',
  'dispatched' => 'start',
  'in_progress' => 'pause',
  'paused' => 'resume',
  _ => null,
};

String actionLabel(String action) => switch (action) {
  'accept' => 'Accept Work',
  'en_route' => 'En Route',
  'arrived' => 'Arrived',
  'start' => 'Start Work',
  'pause' => 'Pause Work',
  'hold' => 'Pause Work',
  'resume' => 'Resume Work',
  'complete' => 'Complete Work',
  _ => action,
};

String statusLabel(String status) => switch (status) {
  'in_progress' => 'In Progress',
  'paused' => 'Paused',
  'completed' => 'Completed',
  _ => status.replaceAll('_', ' '),
};

DateTime? _date(Object? value) =>
    value is String ? DateTime.tryParse(value) : null;

List<Map<String, dynamic>> _mapList(Object? value) {
  final items = switch (value) {
    final List list => list,
    {'items': final List list} => list,
    _ => const [],
  };
  return [
    for (final item in items)
      if (item is Map) item.cast<String, dynamic>(),
  ];
}
