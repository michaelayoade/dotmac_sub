/// Mirrors QuotaBucketRead from app/schemas/usage.py.
class QuotaBucket {
  QuotaBucket({
    required this.id,
    required this.subscriptionId,
    required this.periodStart,
    required this.periodEnd,
    this.includedGb,
    this.usedGb = 0,
    this.rolloverGb = 0,
    this.overageGb = 0,
  });

  final String id;
  final String subscriptionId;
  final DateTime periodStart;
  final DateTime periodEnd;
  final double? includedGb;
  final double usedGb;
  final double rolloverGb;
  final double overageGb;

  /// Total data available this period (included + rolled-over).
  double? get allowanceGb =>
      includedGb == null ? null : includedGb! + rolloverGb;

  double? get remainingGb {
    final a = allowanceGb;
    if (a == null) return null;
    final r = a - usedGb;
    return r < 0 ? 0 : r;
  }

  /// 0..1 fraction of the allowance consumed, or null for unlimited plans.
  double? get usedFraction {
    final a = allowanceGb;
    if (a == null || a <= 0) return null;
    final f = usedGb / a;
    return f.clamp(0.0, 1.0);
  }

  bool get isUnlimited => includedGb == null;

  factory QuotaBucket.fromJson(Map<String, dynamic> json) => QuotaBucket(
        id: json['id'].toString(),
        subscriptionId: json['subscription_id'].toString(),
        periodStart: DateTime.parse(json['period_start'].toString()).toLocal(),
        periodEnd: DateTime.parse(json['period_end'].toString()).toLocal(),
        includedGb: _toDoubleOrNull(json['included_gb']),
        usedGb: _toDouble(json['used_gb']),
        rolloverGb: _toDouble(json['rollover_gb']),
        overageGb: _toDouble(json['overage_gb']),
      );
}

/// Mirrors RadiusAccountingSessionRead from app/schemas/usage.py.
class AccountingSession {
  AccountingSession({
    required this.id,
    required this.subscriptionId,
    required this.sessionId,
    required this.statusType,
    this.sessionStart,
    this.sessionEnd,
    this.inputOctets,
    this.outputOctets,
    this.terminateCause,
  });

  final String id;
  final String subscriptionId;
  final String sessionId;
  final String statusType;
  final DateTime? sessionStart;
  final DateTime? sessionEnd;
  final int? inputOctets;
  final int? outputOctets;
  final String? terminateCause;

  /// Total bytes transferred (down + up). RADIUS input = from the NAS toward
  /// the subscriber's perspective varies by vendor; we just sum both.
  int get totalOctets => (inputOctets ?? 0) + (outputOctets ?? 0);

  bool get isActive => sessionEnd == null;

  factory AccountingSession.fromJson(Map<String, dynamic> json) =>
      AccountingSession(
        id: json['id'].toString(),
        subscriptionId: json['subscription_id'].toString(),
        sessionId: json['session_id'].toString(),
        statusType: json['status_type'].toString(),
        sessionStart: _toDate(json['session_start']),
        sessionEnd: _toDate(json['session_end']),
        inputOctets: (json['input_octets'] as num?)?.toInt(),
        outputOctets: (json['output_octets'] as num?)?.toInt(),
        terminateCause: json['terminate_cause'] as String?,
      );
}

double _toDouble(dynamic v) {
  if (v == null) return 0;
  if (v is num) return v.toDouble();
  return double.tryParse(v.toString()) ?? 0;
}

double? _toDoubleOrNull(dynamic v) {
  if (v == null) return null;
  if (v is num) return v.toDouble();
  return double.tryParse(v.toString());
}

DateTime? _toDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}
