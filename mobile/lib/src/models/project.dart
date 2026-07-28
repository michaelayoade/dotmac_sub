// Typed native customer-experience lifecycle from `/me/projects`.

import 'status_presentation.dart';

class ProjectTicketLink {
  const ProjectTicketLink({
    required this.id,
    required this.title,
    required this.status,
    this.number,
  });

  final String id;
  final String title;
  final String status;
  final String? number;

  factory ProjectTicketLink.fromJson(Map<String, dynamic> json) =>
      ProjectTicketLink(
        id: json['id'].toString(),
        title: (json['title'] ?? '').toString(),
        status: (json['status'] ?? '').toString(),
        number: json['number'] as String?,
      );
}

class ProjectWorkOrderLink {
  const ProjectWorkOrderLink({
    required this.id,
    required this.publicId,
    required this.title,
    required this.status,
  });

  final String id;
  final String publicId;
  final String title;
  final String status;

  factory ProjectWorkOrderLink.fromJson(Map<String, dynamic> json) =>
      ProjectWorkOrderLink(
        id: json['id'].toString(),
        publicId: json['public_id'].toString(),
        title: (json['title'] ?? '').toString(),
        status: (json['status'] ?? '').toString(),
      );
}

class ProjectStage {
  ProjectStage({
    required this.title,
    required this.status,
    this.key,
    this.taskId,
    this.completedAt,
    this.ticket,
    this.workOrders = const [],
  });

  final String? key;
  final String? taskId;
  final String title;
  final String status; // pending | in_progress | done
  final DateTime? completedAt;
  final ProjectTicketLink? ticket;
  final List<ProjectWorkOrderLink> workOrders;

  factory ProjectStage.fromJson(Map<String, dynamic> json) => ProjectStage(
        key: json['key'] as String?,
        taskId: json['task_id']?.toString(),
        title: (json['title'] ?? '').toString(),
        status: (json['status'] ?? 'pending').toString(),
        completedAt: _asDate(json['completed_at']),
        ticket: json['ticket'] is Map
            ? ProjectTicketLink.fromJson(
                (json['ticket'] as Map).cast<String, dynamic>(),
              )
            : null,
        workOrders: ((json['work_orders'] as List?) ?? const [])
            .whereType<Map>()
            .map(
              (item) =>
                  ProjectWorkOrderLink.fromJson(item.cast<String, dynamic>()),
            )
            .toList(),
      );
}

class ProjectItem {
  ProjectItem({
    required this.id,
    required this.name,
    required this.status,
    required this.progressPct,
    required this.experienceState,
    required this.stages,
    this.projectType,
    this.currentStage,
    this.customerAddress,
    this.region,
    this.createdAt,
    StatusPresentation? statusPresentation,
  }) : statusPresentation =
            statusPresentation ?? StatusPresentation.neutralFallback(status);

  final String id;
  final String name;
  final String status;
  final StatusPresentation statusPresentation;
  final String? projectType;
  final int progressPct;
  final String experienceState;
  final String? currentStage;
  final List<ProjectStage> stages;
  final String? customerAddress;
  final String? region;
  final DateTime? createdAt;

  factory ProjectItem.fromJson(Map<String, dynamic> json) => ProjectItem(
        id: json['id'].toString(),
        name: (json['name'] ?? '').toString(),
        status: (json['status'] ?? 'open').toString(),
        statusPresentation: json['status_presentation'] is Map
            ? StatusPresentation.fromJson(
                (json['status_presentation'] as Map).cast<String, dynamic>(),
              )
            : null,
        projectType: json['project_type'] as String?,
        progressPct: _asInt(json['progress_pct']),
        experienceState: (json['experience_state'] ?? 'planned').toString(),
        currentStage: json['current_stage'] as String?,
        stages: ((json['stages'] as List?) ?? const [])
            .whereType<Map<String, dynamic>>()
            .map(ProjectStage.fromJson)
            .toList(),
        customerAddress: json['customer_address'] as String?,
        region: json['region'] as String?,
        createdAt: _asDate(json['created_at']),
      );
}

class ProjectsSummary {
  ProjectsSummary({
    required this.projects,
    required this.total,
    required this.active,
  });

  final List<ProjectItem> projects;
  final int total;
  final int active;

  factory ProjectsSummary.fromJson(Map<String, dynamic> json) =>
      ProjectsSummary(
        projects: ((json['projects'] as List?) ?? const [])
            .whereType<Map<String, dynamic>>()
            .map(ProjectItem.fromJson)
            .toList(),
        total: _asInt(json['total']),
        active: _asInt(json['active']),
      );
}

int _asInt(dynamic v) {
  if (v is int) return v;
  return int.tryParse(v?.toString() ?? '') ?? 0;
}

DateTime? _asDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}
