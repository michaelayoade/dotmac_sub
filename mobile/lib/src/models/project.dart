// Installation tracker models — mirror of the sub `/me/projects` payload.

class ProjectStage {
  ProjectStage({
    required this.title,
    required this.status,
    this.key,
    this.completedAt,
  });

  final String? key;
  final String title;
  final String status; // pending | in_progress | done
  final DateTime? completedAt;

  factory ProjectStage.fromJson(Map<String, dynamic> json) => ProjectStage(
        key: json['key'] as String?,
        title: (json['title'] ?? '').toString(),
        status: (json['status'] ?? 'pending').toString(),
        completedAt: _asDate(json['completed_at']),
      );
}

class ProjectItem {
  ProjectItem({
    required this.id,
    required this.name,
    required this.status,
    required this.progressPct,
    required this.stages,
    this.projectType,
    this.currentStage,
    this.customerAddress,
    this.region,
    this.createdAt,
  });

  final String id;
  final String name;
  final String status;
  final String? projectType;
  final int progressPct;
  final String? currentStage;
  final List<ProjectStage> stages;
  final String? customerAddress;
  final String? region;
  final DateTime? createdAt;

  factory ProjectItem.fromJson(Map<String, dynamic> json) => ProjectItem(
        id: json['id'].toString(),
        name: (json['name'] ?? '').toString(),
        status: (json['status'] ?? 'open').toString(),
        projectType: json['project_type'] as String?,
        progressPct: _asInt(json['progress_pct']),
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
