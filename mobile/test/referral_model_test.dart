import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/models/referral.dart';

void main() {
  group('ReferralSummary.fromJson', () {
    test('parses code, share link, program, totals and history', () {
      final summary = ReferralSummary.fromJson({
        'code': 'DOTMAC-AB12',
        'share_url': 'https://app.dotmac.io/r/DOTMAC-AB12',
        'program': {
          'enabled': true,
          'reward_amount': '5000.00',
          'reward_currency': 'NGN',
        },
        'totals': {
          'total': 2,
          'pending': 1,
          'qualified': 0,
          'rewarded': 1,
          'total_earned': '5000.00',
        },
        'referrals': [
          {
            'id': 'r1',
            'status': 'rewarded',
            'referred_name': 'Ada',
            'reward_amount': '5000.00',
            'reward_currency': 'NGN',
            'reward_status': 'paid',
            'created_at': '2026-06-01T10:00:00+00:00',
          },
          {'id': 'r2', 'status': 'pending'},
        ],
      });

      expect(summary.code, 'DOTMAC-AB12');
      expect(summary.shareUrl.endsWith('/r/DOTMAC-AB12'), isTrue);
      expect(summary.program.enabled, isTrue);
      expect(summary.program.rewardAmount, '5000.00');
      expect(summary.totals.total, 2);
      expect(summary.totals.rewarded, 1);
      expect(summary.totals.totalEarned, '5000.00');
      expect(summary.referrals.length, 2);
      expect(summary.referrals.first.referredName, 'Ada');
      expect(summary.referrals.first.createdAt, isNotNull);
      expect(summary.referrals.last.status, 'pending');
    });

    test('tolerates missing program/totals/referrals', () {
      final summary = ReferralSummary.fromJson({'code': 'X', 'share_url': ''});
      expect(summary.program.enabled, isFalse);
      expect(summary.program.rewardAmount, '0');
      expect(summary.totals.total, 0);
      expect(summary.referrals, isEmpty);
    });
  });
}
