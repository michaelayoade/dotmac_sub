// Typed native field-service lifecycle from `/me/work-orders`.

import 'status_presentation.dart';

class WorkOrderSelfCareAction {
  const WorkOrderSelfCareAction({required this.key, required this.allowed});

  final String key;
  final bool allowed;

  factory WorkOrderSelfCareAction.fromJson(Map<String, dynamic> json) =>
      WorkOrderSelfCareAction(
        key: (json['key'] ?? '').toString(),
        allowed: json['allowed'] as bool? ?? false,
      );
}

class WorkOrderItem {
  WorkOrderItem({
    required this.id,
    required this.publicId,
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
    this.projectId,
    this.projectName,
    this.projectTaskId,
    this.projectTaskTitle,
    this.originTicketId,
    this.originTicketNumber,
    this.actions = const [],
    StatusPresentation? statusPresentation,
  }) : statusPresentation =
           statusPresentation ?? StatusPresentation.neutralFallback(status);

  final String id;
  final String publicId;
  final String title;
  final String status;
  final StatusPresentation statusPresentation;
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
  final String? projectId;
  final String? projectName;
  final String? projectTaskId;
  final String? projectTaskTitle;
  final String? originTicketId;
  final String? originTicketNumber;
  final List<WorkOrderSelfCareAction> actions;

  bool get canTrackTechnician => actions.any(
    (action) => action.key == 'track_technician' && action.allowed,
  );

  bool get canRateTechnician => actions.any(
    (action) => action.key == 'rate_technician' && action.allowed,
  );

  factory WorkOrderItem.fromJson(Map<String, dynamic> json) => WorkOrderItem(
    id: json['id'].toString(),
    publicId: json['public_id'].toString(),
    title: (json['title'] ?? '').toString(),
    status: (json['status'] ?? 'scheduled').toString(),
    statusPresentation: json['status_presentation'] is Map
        ? StatusPresentation.fromJson(
            (json['status_presentation'] as Map).cast<String, dynamic>(),
          )
        : null,
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
    projectId: json['project_id']?.toString(),
    projectName: json['project_name'] as String?,
    projectTaskId: json['project_task_id']?.toString(),
    projectTaskTitle: json['project_task_title'] as String?,
    originTicketId: json['origin_ticket_id']?.toString(),
    originTicketNumber: json['origin_ticket'] is Map
        ? (json['origin_ticket'] as Map)['number'] as String?
        : null,
    actions: ((json['actions'] as List?) ?? const [])
        .whereType<Map>()
        .map(
          (item) =>
              WorkOrderSelfCareAction.fromJson(item.cast<String, dynamic>()),
        )
        .toList(),
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
