// Mirrors QuotaBucketRead from app/schemas/usage.py.
import '../core/parsers.dart';

class QuotaBucket {
  QuotaBucket({
    required this.id,
    required this.subscriptionId,
    required this.periodStart,
    required this.periodEnd,
    this.includedGb,
    this.usedGb = 0,
    this.rolloverGb = 0,
    this.topupGb = 0,
    this.overageGb = 0,
    this.overageAmount,
  });

  final String id;
  final String subscriptionId;
  final DateTime periodStart;
  final DateTime periodEnd;
  final double? includedGb;
  final double usedGb;
  final double rolloverGb;
  final double topupGb;
  final double overageGb;

  /// Running cost of the current overage (₦), when the plan has an overage
  /// rate. Null when not in overage or unmetered.
  final double? overageAmount;

  /// Total data available this period (included + rolled-over + top-ups).
  double? get allowanceGb =>
      isUnlimited ? null : includedGb! + rolloverGb + topupGb;

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

  /// Unmetered plan: no included allowance on file (null) or an explicit 0,
  /// which is how "unlimited" offers are rated (no GB cap to count against).
  bool get isUnlimited => includedGb == null || includedGb! <= 0;

  factory QuotaBucket.fromJson(Map<String, dynamic> json) => QuotaBucket(
        id: json['id'].toString(),
        subscriptionId: json['subscription_id'].toString(),
        periodStart: DateTime.parse(json['period_start'].toString()).toLocal(),
        periodEnd: DateTime.parse(json['period_end'].toString()).toLocal(),
        includedGb: asDoubleOrNull(json['included_gb']),
        usedGb: asDouble(json['used_gb']),
        rolloverGb: asDouble(json['rollover_gb']),
        topupGb: asDouble(json['topup_gb']),
        overageGb: asDouble(json['overage_gb']),
        overageAmount: asDoubleOrNull(json['overage_amount']),
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
    this.lastUpdateAt,
    this.inputOctets,
    this.outputOctets,
    this.terminateCause,
    this.framedIpAddress,
  });

  final String id;
  final String subscriptionId;
  final String sessionId;
  final String statusType;
  final DateTime? sessionStart;
  final DateTime? sessionEnd;

  /// Most recent accounting observation (interim update or stop). For an
  /// active session this is when it was last seen alive — connect time alone
  /// can be days old on a healthy always-on PPPoE session.
  final DateTime? lastUpdateAt;
  final int? inputOctets;
  final int? outputOctets;
  final String? terminateCause;

  /// The IP the NAS assigned this session (RADIUS Framed-IP-Address) — the
  /// subscriber's live address, present even for dynamically-addressed plans.
  final String? framedIpAddress;

  /// Total bytes transferred (down + up). RADIUS input = from the NAS toward
  /// the subscriber's perspective varies by vendor; we just sum both.
  int get totalOctets => (inputOctets ?? 0) + (outputOctets ?? 0);

  bool get isActive => sessionEnd == null;

  DateTime? get lastSeenAt => lastUpdateAt ?? sessionEnd ?? sessionStart;

  factory AccountingSession.fromJson(Map<String, dynamic> json) =>
      AccountingSession(
        id: json['id'].toString(),
        subscriptionId: json['subscription_id'].toString(),
        sessionId: json['session_id'].toString(),
        statusType: json['status_type'].toString(),
        sessionStart: _toDate(json['session_start']),
        sessionEnd: _toDate(json['session_end']),
        lastUpdateAt: _toDate(json['last_update_at']),
        inputOctets: (json['input_octets'] as num?)?.toInt(),
        outputOctets: (json['output_octets'] as num?)?.toInt(),
        terminateCause: json['terminate_cause'] as String?,
        framedIpAddress: json['framed_ip_address'] as String?,
      );
}

/// Live throughput for the customer's active subscription, subscriber
/// perspective. Mirrors BandwidthStats from api/bandwidth.py (we bind only
/// the download/upload fields — rx/tx are NAS-perspective).
class LiveBandwidth {
  LiveBandwidth({
    this.downloadBps,
    this.uploadBps,
    this.peakDownloadBps,
    this.peakUploadBps,
  });

  final double? downloadBps;
  final double? uploadBps;

  /// Peak throughput over the requested window (subscriber perspective).
  /// Populated by /bandwidth/my/stats; null on the live one-shot.
  final double? peakDownloadBps;
  final double? peakUploadBps;

  bool get hasSignal => (downloadBps ?? 0) > 0 || (uploadBps ?? 0) > 0;

  factory LiveBandwidth.fromJson(Map<String, dynamic> json) => LiveBandwidth(
        downloadBps: (json['download_bps'] as num?)?.toDouble(),
        uploadBps: (json['upload_bps'] as num?)?.toDouble(),
        peakDownloadBps: (json['peak_download_bps'] as num?)?.toDouble(),
        peakUploadBps: (json['peak_upload_bps'] as num?)?.toDouble(),
      );
}

/// One point of the bandwidth-speed time series. Mirrors BandwidthSeriesPoint
/// from app/api/bandwidth.py (GET /bandwidth/my/series). We bind only the
/// subscriber-perspective download/upload rates.
class BandwidthPoint {
  BandwidthPoint({
    required this.at,
    required this.downloadBps,
    required this.uploadBps,
  });

  final DateTime at;
  final double downloadBps;
  final double uploadBps;

  double get totalBps => downloadBps + uploadBps;

  factory BandwidthPoint.fromJson(Map<String, dynamic> json) => BandwidthPoint(
        at: DateTime.parse(json['timestamp'].toString()).toLocal(),
        downloadBps: (json['download_bps'] as num?)?.toDouble() ?? 0,
        uploadBps: (json['upload_bps'] as num?)?.toDouble() ?? 0,
      );
}

/// One bar of the usage chart. Mirrors UsageSeriesPoint from schemas/usage.py.
class UsageSeriesPoint {
  UsageSeriesPoint({required this.bucketStart, required this.bytes});

  final DateTime bucketStart;
  final int bytes;

  factory UsageSeriesPoint.fromJson(Map<String, dynamic> json) =>
      UsageSeriesPoint(
        bucketStart: DateTime.parse(json['bucket_start'].toString()).toLocal(),
        bytes: (json['bytes'] as num?)?.toInt() ?? 0,
      );
}

/// Customer-facing Fair-Usage status. Mirrors FupSummary in schemas/usage.py.
class FupStatus {
  FupStatus({
    required this.status,
    this.isReduced = false,
    this.speedReductionPercent,
    this.activeRuleName,
    this.resetsAt,
    this.summary,
    this.thresholdGb,
    this.usedGb,
    this.gbUntilThrottle,
    this.usageRatio,
    this.policySummary,
  });

  final String status; // full_speed | approaching | throttled | blocked
  final bool isReduced;
  final double? speedReductionPercent;
  final String? activeRuleName;
  final DateTime? resetsAt;
  final String? summary; // plain-language explainer

  /// Headroom against the nearest throttle/block rule — present even while
  /// healthy so the app can pre-warn before enforcement.
  final double? thresholdGb;
  final double? usedGb;
  final double? gbUntilThrottle;
  final double? usageRatio;

  /// Policy terms shown regardless of state, e.g.
  /// "Speed reduces to 25% after 500 GB each month".
  final String? policySummary;

  bool get isThrottled => status == 'throttled';
  bool get isBlocked => status == 'blocked';
  bool get isApproaching => status == 'approaching';

  /// Whether the customer should see a banner / restore CTA.
  bool get needsAttention => isThrottled || isBlocked;

  factory FupStatus.fromJson(Map<String, dynamic> json) => FupStatus(
        status: json['status']?.toString() ?? 'full_speed',
        isReduced: json['is_reduced'] as bool? ?? false,
        speedReductionPercent:
            (json['speed_reduction_percent'] as num?)?.toDouble(),
        activeRuleName: json['active_rule_name'] as String?,
        resetsAt: _toDate(json['resets_at']),
        summary: json['summary'] as String?,
        thresholdGb: (json['threshold_gb'] as num?)?.toDouble(),
        usedGb: (json['used_gb'] as num?)?.toDouble(),
        gbUntilThrottle: (json['gb_until_throttle'] as num?)?.toDouble(),
        usageRatio: (json['usage_ratio'] as num?)?.toDouble(),
        policySummary: json['policy_summary'] as String?,
      );
}

/// Windowed data-usage summary. Mirrors UsageSummaryResponse from
/// schemas/usage.py (GET /me/usage-summary).
class UsageSummary {
  UsageSummary({
    required this.period,
    required this.start,
    required this.end,
    required this.totalBytes,
    required this.totalSource,
    required this.isAuthoritative,
    this.bucket,
    this.averageBps,
    this.peakDownloadBps,
    this.peakUploadBps,
    this.series = const [],
    this.fup,
  });

  final String period; // hour | today | week | cycle | all
  final DateTime start;
  final DateTime end;
  final int totalBytes;
  final String totalSource; // samples | sessions | quota | lifetime
  final bool isAuthoritative;

  /// Billing-grade headline total when the server says this window is
  /// authoritative. Zero is a valid value; null means the loaded summary is
  /// estimated and must not be replaced with a client-side reconstruction.
  int? get authoritativeTotalBytes => isAuthoritative ? totalBytes : null;

  final String? bucket; // minute | hour | day | null

  /// Mean throughput over the window (rx+tx bits/s) — the "average speed".
  /// Null for windows with no samples (e.g. "all").
  final double? averageBps;

  /// Exact peak throughput over the window (subscriber bits/s). Populated for
  /// the billing cycle; null when unavailable.
  final double? peakDownloadBps;
  final double? peakUploadBps;
  final List<UsageSeriesPoint> series;
  final FupStatus? fup;

  factory UsageSummary.fromJson(Map<String, dynamic> json) => UsageSummary(
        period: json['period'].toString(),
        start: DateTime.parse(json['start'].toString()).toLocal(),
        end: DateTime.parse(json['end'].toString()).toLocal(),
        totalBytes: (json['total_bytes'] as num?)?.toInt() ?? 0,
        totalSource: json['total_source'].toString(),
        isAuthoritative: json['is_authoritative'] as bool? ?? false,
        bucket: json['bucket'] as String?,
        averageBps: (json['average_bps'] as num?)?.toDouble(),
        peakDownloadBps: (json['peak_download_bps'] as num?)?.toDouble(),
        peakUploadBps: (json['peak_upload_bps'] as num?)?.toDouble(),
        series: (json['series'] as List? ?? const [])
            .map((e) => UsageSeriesPoint.fromJson(e as Map<String, dynamic>))
            .toList(),
        fup: json['fup'] == null
            ? null
            : FupStatus.fromJson(json['fup'] as Map<String, dynamic>),
      );
}

/// One calendar day's traffic (bytes). Mirrors DailyUsagePoint from
/// schemas/usage.py (GET /me/usage-history). Dates are plain calendar days —
/// not localized, so month bucketing stays stable across timezones.
class DailyUsagePoint {
  DailyUsagePoint({
    required this.date,
    required this.uploadBytes,
    required this.downloadBytes,
    required this.totalBytes,
  });

  final DateTime date;
  final int uploadBytes;
  final int downloadBytes;
  final int totalBytes;

  factory DailyUsagePoint.fromJson(Map<String, dynamic> json) =>
      DailyUsagePoint(
        date: DateTime.parse(json['date'].toString()),
        uploadBytes: (json['upload_bytes'] as num?)?.toInt() ?? 0,
        downloadBytes: (json['download_bytes'] as num?)?.toInt() ?? 0,
        totalBytes: (json['total_bytes'] as num?)?.toInt() ?? 0,
      );
}

/// One calendar month's total traffic (bytes), aggregated client-side.
class MonthlyUsagePoint {
  MonthlyUsagePoint({required this.month, required this.bytes});

  /// First day of the month (day = 1).
  final DateTime month;
  final int bytes;
}

/// Long-history daily usage. Mirrors DailyUsageHistoryResponse from
/// schemas/usage.py (GET /me/usage-history). Sourced from the daily rollup
/// (Splynx backfill), reaching years further back than per-session accounting.
class UsageHistory {
  UsageHistory({
    required this.start,
    required this.end,
    required this.totalUploadBytes,
    required this.totalDownloadBytes,
    required this.totalBytes,
    this.points = const [],
  });

  final DateTime start;
  final DateTime end;
  final int totalUploadBytes;
  final int totalDownloadBytes;
  final int totalBytes;
  final List<DailyUsagePoint> points;

  factory UsageHistory.fromJson(Map<String, dynamic> json) => UsageHistory(
        start: DateTime.parse(json['start'].toString()),
        end: DateTime.parse(json['end'].toString()),
        totalUploadBytes: (json['total_upload_bytes'] as num?)?.toInt() ?? 0,
        totalDownloadBytes:
            (json['total_download_bytes'] as num?)?.toInt() ?? 0,
        totalBytes: (json['total_bytes'] as num?)?.toInt() ?? 0,
        points: (json['points'] as List? ?? const [])
            .map((e) => DailyUsagePoint.fromJson(e as Map<String, dynamic>))
            .toList(),
      );

  /// Aggregate the daily points into calendar-month totals, ascending. Gaps
  /// (months with no recorded usage) are simply absent — same as the daily
  /// source, which isn't zero-filled.
  List<MonthlyUsagePoint> toMonthly() {
    final byMonth = <String, int>{};
    for (final p in points) {
      final key = '${p.date.year}-${p.date.month.toString().padLeft(2, '0')}';
      byMonth[key] = (byMonth[key] ?? 0) + p.totalBytes;
    }
    final out = byMonth.entries.map((e) {
      final parts = e.key.split('-');
      return MonthlyUsagePoint(
        month: DateTime(int.parse(parts[0]), int.parse(parts[1])),
        bytes: e.value,
      );
    }).toList()
      ..sort((a, b) => a.month.compareTo(b.month));
    return out;
  }
}

DateTime? _toDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}
