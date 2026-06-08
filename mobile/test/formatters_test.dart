import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/core/formatters.dart';

void main() {
  group('Fmt.compactDuration', () {
    test('picks the largest single unit', () {
      expect(Fmt.compactDuration(const Duration(days: 2, hours: 5)), '2d');
      expect(Fmt.compactDuration(const Duration(hours: 3, minutes: 40)), '3h');
      expect(Fmt.compactDuration(const Duration(minutes: 12)), '12m');
      expect(Fmt.compactDuration(const Duration(seconds: 30)), 'just now');
      expect(Fmt.compactDuration(Duration.zero), 'just now');
    });
  });

  group('Fmt.uptime', () {
    test('formats a past start as a compact uptime', () {
      final start = DateTime.now().subtract(const Duration(hours: 3));
      expect(Fmt.uptime(start), '3h');
    });

    test('future/clock-skew start does not throw and reads sanely', () {
      final future = DateTime.now().add(const Duration(minutes: 5));
      expect(Fmt.uptime(future), 'just now');
    });
  });
}
