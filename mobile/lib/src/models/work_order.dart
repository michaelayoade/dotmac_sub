// Field-service models — mirror of the sub `/me/work-orders` payload.

class WorkOrderItem {
  WorkOrderItem({
    required this.id,
    required this.title,
    required this.status,
    this.workType,
    this.priority,
    this.technicianName,
    this.technicianPhone,
    this.address,
    this.scheduledStart,
    this.scheduledEnd,
    this.estimatedArrivalAt,
    this.estimatedDurationMinutes,
    this.completedAt,
  });

  final String id;
  final String title;
  final String status;
  final String? workType;
  final String? priority;
  final String? technicianName;
  final String? technicianPhone;
  final String? address;
  final DateTime? scheduledStart;
  final DateTime? scheduledEnd;
  final DateTime? estimatedArrivalAt;
  final int? estimatedDurationMinutes;
  final DateTime? completedAt;

  factory WorkOrderItem.fromJson(Map<String, dynamic> json) => WorkOrderItem(
    id: json['id'].toString(),
    title: (json['title'] ?? '').toString(),
    status: (json['status'] ?? 'scheduled').toString(),
    workType: json['work_type'] as String?,
    priority: json['priority'] as String?,
    technicianName: json['technician_name'] as String?,
    technicianPhone: json['technician_phone'] as String?,
    address: json['address'] as String?,
    scheduledStart: _asDate(json['scheduled_start']),
    scheduledEnd: _asDate(json['scheduled_end']),
    estimatedArrivalAt: _asDate(json['estimated_arrival_at']),
    estimatedDurationMinutes: _asIntOrNull(json['estimated_duration_minutes']),
    completedAt: _asDate(json['completed_at']),
  );
}

class WorkOrdersSummary {
  WorkOrdersSummary({
    required this.workOrders,
    required this.total,
    required this.upcoming,
  });

  final List<WorkOrderItem> workOrders;
  final int total;
  final int upcoming;

  factory WorkOrdersSummary.fromJson(Map<String, dynamic> json) =>
      WorkOrdersSummary(
        workOrders: ((json['work_orders'] as List?) ?? const [])
            .whereType<Map<String, dynamic>>()
            .map(WorkOrderItem.fromJson)
            .toList(),
        total: _asInt(json['total']),
        upcoming: _asInt(json['upcoming']),
      );
}

int _asInt(dynamic v) {
  if (v is int) return v;
  return int.tryParse(v?.toString() ?? '') ?? 0;
}

int? _asIntOrNull(dynamic v) {
  if (v == null) return null;
  if (v is int) return v;
  return int.tryParse(v.toString());
}

DateTime? _asDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}
