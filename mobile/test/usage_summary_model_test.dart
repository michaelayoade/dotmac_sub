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
}
