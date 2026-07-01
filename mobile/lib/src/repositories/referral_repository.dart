import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/referral.dart';

/// Wraps the self-scoped Refer & Earn endpoints (app/api/me.py, /me/referrals).
///
/// Reads come from the sub's local referral mirror (fast, resilient to a CRM
/// outage); refer-a-friend writes through to the CRM.
class ReferralRepository {
  ReferralRepository(this.dio);

  final Dio dio;

  /// GET /me/referrals — code, share link, program terms, totals, history.
  Future<ReferralSummary> summary() async {
    final data = await guard(() => dio.get('/me/referrals'));
    return ReferralSummary.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/referrals — refer a friend; returns the server's message.
  Future<String> refer({String? name, String? email, String? phone}) async {
    final data = await guard(
      () => dio.post(
        '/me/referrals',
        data: {
          if (name != null && name.isNotEmpty) 'name': name,
          if (email != null && email.isNotEmpty) 'email': email,
          if (phone != null && phone.isNotEmpty) 'phone': phone,
        },
      ),
    );
    final map = data as Map<String, dynamic>;
    return (map['message'] ?? 'Referral submitted').toString();
  }
}
