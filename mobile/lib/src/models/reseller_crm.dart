/// Reseller views of the CRM mirrors (Sales/Quotes B3 → /reseller/quotes,
/// /reseller/projects, /reseller/work-orders). Each row carries the customer
/// account it belongs to so the reseller sees "which customer".
library;

import 'quote.dart';

String _str(dynamic v) => v == null ? '' : v.toString();

DateTime? _toDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}

Map<String, dynamic>? _asMap(dynamic v) =>
    v is Map ? v.cast<String, dynamic>() : null;

/// A self-serve quote scoped to one of the reseller's customers. Reuses [Quote]
/// for the quote body and adds the owning account.
class ResellerQuote {
  ResellerQuote({
    required this.accountId,
    this.accountName,
    required this.quote,
  });

  final String accountId;
  final String? accountName;
  final Quote quote;

  factory ResellerQuote.fromJson(Map<String, dynamic> json) => ResellerQuote(
    accountId: _str(json['account_id']),
    accountName: json['account_name'] as String?,
    quote: Quote.fromJson(json),
  );
}

class ResellerProject {
  ResellerProject({
    required this.accountId,
    this.accountName,
    required this.id,
    required this.name,
    required this.status,
    this.projectType,
    this.progressPct = 0,
    this.currentStage,
    this.region,
    this.customerAddress,
    this.dueAt,
    this.createdAt,
  });

  final String accountId;
  final String? accountName;
  final String id;
  final String name;
  final String status;
  final String? projectType;
  final int progressPct;
  final String? currentStage;
  final String? region;
  final String? customerAddress;
  final DateTime? dueAt;
  final DateTime? createdAt;

  factory ResellerProject.fromJson(Map<String, dynamic> json) =>
      ResellerProject(
        accountId: _str(json['account_id']),
        accountName: json['account_name'] as String?,
        id: _str(json['id']),
        name: json['name'] as String? ?? 'Installation',
        status: json['status'] as String? ?? 'open',
        projectType: json['project_type'] as String?,
        progressPct: (json['progress_pct'] as num?)?.toInt() ?? 0,
        currentStage: json['current_stage'] as String?,
        region: json['region'] as String?,
        customerAddress: json['customer_address'] as String?,
        dueAt: _toDate(json['due_at']),
        createdAt: _toDate(json['created_at']),
      );
}

class ResellerWorkOrder {
  ResellerWorkOrder({
    required this.accountId,
    this.accountName,
    required this.id,
    required this.title,
    required this.status,
    this.workType,
    this.priority,
    this.technicianName,
    this.technicianPhone,
    this.address,
    this.scheduledStart,
    this.estimatedArrivalAt,
    this.completedAt,
  });

  final String accountId;
  final String? accountName;
  final String id;
  final String title;
  final String status;
  final String? workType;
  final String? priority;
  final String? technicianName;
  final String? technicianPhone;
  final String? address;
  final DateTime? scheduledStart;
  final DateTime? estimatedArrivalAt;
  final DateTime? completedAt;

  factory ResellerWorkOrder.fromJson(Map<String, dynamic> json) =>
      ResellerWorkOrder(
        accountId: _str(json['account_id']),
        accountName: json['account_name'] as String?,
        id: _str(json['id']),
        title: json['title'] as String? ?? 'Work order',
        status: json['status'] as String? ?? 'scheduled',
        workType: json['work_type'] as String?,
        priority: json['priority'] as String?,
        technicianName: json['technician_name'] as String?,
        technicianPhone: json['technician_phone'] as String?,
        address: json['address'] as String?,
        scheduledStart: _toDate(json['scheduled_start']),
        estimatedArrivalAt: _toDate(json['estimated_arrival_at']),
        completedAt: _toDate(json['completed_at']),
      );
}

/// Parse a `{quotes|projects|work_orders: [...]}` reseller envelope into a typed
/// list. (The envelope also carries total/open/active counts.)
List<T> parseResellerList<T>(
  Map<String, dynamic> data,
  String key,
  T Function(Map<String, dynamic>) fromJson,
) {
  final list = data[key] as List? ?? const [];
  return [
    for (final item in list)
      if (_asMap(item) case final m?) fromJson(m),
  ];
}
