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

  /// GET /bandwidth/my/stats — current throughput for the subscriber's active
  /// subscription (subscriber-perspective download/upload).
  Future<LiveBandwidth> liveBandwidth({String period = '1h'}) async {
    final data = await guard(() =>
        dio.get('/bandwidth/my/stats', queryParameters: {'period': period}));
    return LiveBandwidth.fromJson(data as Map<String, dynamic>);
  }
}
