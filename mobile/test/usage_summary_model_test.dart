import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/models/usage.dart';

void main() {
  test('UsageSummary.fromJson parses totals and series', () {
    final json = {
      'period': 'today',
      'start': '2026-06-08T00:00:00+00:00',
      'end': '2026-06-08T12:00:00+00:00',
      'total_bytes': 123456,
      'total_source': 'samples',
      'is_authoritative': false,
      'bucket': 'hour',
      'series': [
        {'bucket_start': '2026-06-08T09:00:00+00:00', 'bytes': 1000},
        {'bucket_start': '2026-06-08T10:00:00+00:00', 'bytes': 2000},
      ],
    };

    final s = UsageSummary.fromJson(json);
    expect(s.period, 'today');
    expect(s.totalBytes, 123456);
    expect(s.totalSource, 'samples');
    expect(s.isAuthoritative, isFalse);
    expect(s.bucket, 'hour');
    expect(s.series, hasLength(2));
    expect(s.series[1].bytes, 2000);
  });

  test('UsageSummary.fromJson parses the fup block when throttled', () {
    final s = UsageSummary.fromJson({
      'period': 'cycle',
      'start': '2026-06-01T00:00:00+00:00',
      'end': '2026-06-30T00:00:00+00:00',
      'total_bytes': 0,
      'total_source': 'quota',
      'is_authoritative': true,
      'bucket': 'day',
      'series': [],
      'fup': {
        'status': 'throttled',
        'is_reduced': true,
        'speed_reduction_percent': 75.0,
        'active_rule_name': 'Monthly 100GB cap',
        'resets_at': '2026-07-01T00:00:00+00:00',
        'summary': 'Speed reduced to 25% after 100 GB this month',
      },
    });
    expect(s.fup, isNotNull);
    expect(s.fup!.status, 'throttled');
    expect(s.fup!.isThrottled, isTrue);
    expect(s.fup!.isBlocked, isFalse);
    expect(s.fup!.needsAttention, isTrue);
    expect(s.fup!.speedReductionPercent, 75.0);
    expect(s.fup!.activeRuleName, 'Monthly 100GB cap');
    expect(s.fup!.resetsAt, isNotNull);
    expect(s.fup!.summary, contains('Speed reduced'));
  });

  test('UsageSummary.fromJson treats a missing fup block as null', () {
    final s = UsageSummary.fromJson({
      'period': 'today',
      'start': '2026-06-08T00:00:00+00:00',
      'end': '2026-06-08T12:00:00+00:00',
      'total_bytes': 1,
      'total_source': 'samples',
      'is_authoritative': false,
      'series': [],
    });
    expect(s.fup, isNull);
  });

  test('FupStatus full_speed does not need attention', () {
    final f = FupStatus.fromJson({'status': 'full_speed', 'is_reduced': false});
    expect(f.needsAttention, isFalse);
    expect(f.isThrottled, isFalse);
  });

  test('UsageSummary.fromJson tolerates a null bucket and empty series', () {
    final s = UsageSummary.fromJson({
      'period': 'all',
      'start': '2026-01-01T00:00:00+00:00',
      'end': '2026-06-08T00:00:00+00:00',
      'total_bytes': 9999,
      'total_source': 'sessions',
      'is_authoritative': true,
      'bucket': null,
      'series': [],
    });
    expect(s.bucket, isNull);
    expect(s.series, isEmpty);
    expect(s.isAuthoritative, isTrue);
  });

  test('authoritative total preserves zero instead of treating it as missing',
      () {
    final s = UsageSummary.fromJson({
      'period': 'cycle',
      'start': '2026-06-01T00:00:00+00:00',
      'end': '2026-06-30T00:00:00+00:00',
      'total_bytes': 0,
      'total_source': 'sessions',
      'is_authoritative': true,
      'bucket': 'day',
      'series': [
        {'bucket_start': '2026-06-08T00:00:00+00:00', 'bytes': 9999},
      ],
    });

    expect(s.authoritativeTotalBytes, 0);
  });

  test('estimated total is not eligible for an authoritative headline', () {
    final s = UsageSummary.fromJson({
      'period': 'cycle',
      'start': '2026-06-01T00:00:00+00:00',
      'end': '2026-06-30T00:00:00+00:00',
      'total_bytes': 9999,
      'total_source': 'samples',
      'is_authoritative': false,
      'bucket': 'day',
      'series': [],
    });

    expect(s.authoritativeTotalBytes, isNull);
  });
}
