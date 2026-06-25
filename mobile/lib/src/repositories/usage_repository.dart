import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/page.dart';
import '../models/usage.dart';

/// Wraps the usage endpoints (app/api/domains_usage.py, mounted at /api/v1).
class UsageRepository {
  UsageRepository(this.dio);

  final Dio dio;

  /// GET /me/quota-buckets — all quota buckets across the subscriber's own
  /// subscriptions, in a single round-trip (self-scoped).
  Future<Page<QuotaBucket>> quotaBuckets(
      {int limit = 100, int offset = 0}) async {
    final data =
        await guard(() => dio.get('/me/quota-buckets', queryParameters: {
              'limit': limit,
              'offset': offset,
            }));
    return Page.fromJson(data as Map<String, dynamic>, QuotaBucket.fromJson);
  }

  /// GET /me/radius-accounting-sessions — the subscriber's own data-usage
  /// (RADIUS accounting) sessions, newest first.
  Future<Page<AccountingSession>> sessions(
      {int limit = 50, int offset = 0}) async {
    final data = await guard(
        () => dio.get('/me/radius-accounting-sessions', queryParameters: {
              'limit': limit,
              'offset': offset,
            }));
    return Page.fromJson(
        data as Map<String, dynamic>, AccountingSession.fromJson);
  }

  /// GET /me/usage-summary?period=… — windowed total + bucketed series, with a
  /// defined time window (unlike summing the latest sessions).
  Future<UsageSummary> usageSummary(String period) async {
    final data = await guard(() =>
        dio.get('/me/usage-summary', queryParameters: {'period': period}));
    return UsageSummary.fromJson(data as Map<String, dynamic>);
  }

  /// GET /me/usage-history?days=N — long-history daily upload/download series
  /// (back to 2018 for migrated accounts), summed across the subscriber's
  /// subscriptions. Aggregated to months client-side for the trend chart.
  Future<UsageHistory> usageHistory({int days = 365}) async {
    final data = await guard(
        () => dio.get('/me/usage-history', queryParameters: {'days': days}));
    return UsageHistory.fromJson(data as Map<String, dynamic>);
  }

  /// GET /bandwidth/my/series — bandwidth-speed time series for the caller's
  /// subscription over [start]..[end]. Source auto-selects Postgres (<24h) or
  /// VictoriaMetrics (older), so history reaches as far back as VM retention.
  Future<List<BandwidthPoint>> bandwidthSeries({
    required DateTime start,
    required DateTime end,
    String interval = 'auto',
  }) async {
    final data = await guard(
      () => dio.get('/bandwidth/my/series', queryParameters: {
        'start_at': start.toUtc().toIso8601String(),
        'end_at': end.toUtc().toIso8601String(),
        'interval': interval,
      }),
    );
    final list = (data as Map<String, dynamic>)['data'] as List? ?? const [];
    return list
        .map((e) => BandwidthPoint.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// GET /bandwidth/my/stats — current throughput for the subscriber's active
  /// subscription (subscriber-perspective download/upload).
  Future<LiveBandwidth> liveBandwidth({String period = '1h'}) async {
    final data = await guard(() =>
        dio.get('/bandwidth/my/stats', queryParameters: {'period': period}));
    return LiveBandwidth.fromJson(data as Map<String, dynamic>);
  }

  /// Live throughput as a stream — emits immediately, then re-polls every
  /// [interval] so the connection banner tracks current speed. Backend live
  /// data advances at ~30s (the MikroTik poller cadence), so the poll is paced
  /// to that rather than spinning faster for no new data. A failed tick (e.g.
  /// no active subscription) yields a no-signal value rather than terminating
  /// the stream; autoDispose on the provider stops polling when the dashboard
  /// goes away.
  Stream<LiveBandwidth> liveBandwidthStream({
    Duration interval = const Duration(seconds: 15),
    String period = '1h',
  }) async* {
    Future<LiveBandwidth> tick() async {
      try {
        return await liveBandwidth(period: period);
      } catch (_) {
        return LiveBandwidth();
      }
    }

    yield await tick();
    yield* Stream.periodic(interval).asyncMap((_) => tick());
  }
}
