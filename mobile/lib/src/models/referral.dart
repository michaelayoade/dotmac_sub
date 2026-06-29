// Refer & Earn models — mirror of the sub `/me/referrals` payload (RFC #73).

class ReferralProgram {
  ReferralProgram({
    required this.enabled,
    required this.rewardAmount,
    required this.rewardCurrency,
  });

  final bool enabled;
  final String rewardAmount; // decimal string, e.g. "5000.00"
  final String rewardCurrency;

  factory ReferralProgram.fromJson(Map<String, dynamic> json) =>
      ReferralProgram(
        enabled: json['enabled'] == true,
        rewardAmount: (json['reward_amount'] ?? '0').toString(),
        rewardCurrency: (json['reward_currency'] ?? 'NGN').toString(),
      );
}

class ReferralTotals {
  ReferralTotals({
    this.total = 0,
    this.pending = 0,
    this.qualified = 0,
    this.rewarded = 0,
    this.totalEarned = '0',
  });

  final int total;
  final int pending;
  final int qualified;
  final int rewarded;
  final String totalEarned;

  factory ReferralTotals.fromJson(Map<String, dynamic> json) => ReferralTotals(
    total: _asInt(json['total']),
    pending: _asInt(json['pending']),
    qualified: _asInt(json['qualified']),
    rewarded: _asInt(json['rewarded']),
    totalEarned: (json['total_earned'] ?? '0').toString(),
  );
}

class ReferralItem {
  ReferralItem({
    required this.id,
    required this.status,
    required this.rewardCurrency,
    required this.rewardStatus,
    this.referredName,
    this.rewardAmount,
    this.createdAt,
  });

  final String id;
  final String status; // pending | qualified | rewarded | rejected
  final String? referredName;
  final String? rewardAmount;
  final String rewardCurrency;
  final String rewardStatus;
  final DateTime? createdAt;

  factory ReferralItem.fromJson(Map<String, dynamic> json) => ReferralItem(
    id: json['id'].toString(),
    status: (json['status'] ?? 'pending').toString(),
    referredName: json['referred_name'] as String?,
    rewardAmount: json['reward_amount']?.toString(),
    rewardCurrency: (json['reward_currency'] ?? 'NGN').toString(),
    rewardStatus: (json['reward_status'] ?? 'none').toString(),
    createdAt: _asDate(json['created_at']),
  );
}

class ReferralSummary {
  ReferralSummary({
    required this.code,
    required this.shareUrl,
    required this.program,
    required this.totals,
    required this.referrals,
  });

  final String code;
  final String shareUrl;
  final ReferralProgram program;
  final ReferralTotals totals;
  final List<ReferralItem> referrals;

  factory ReferralSummary.fromJson(Map<String, dynamic> json) =>
      ReferralSummary(
        code: (json['code'] ?? '').toString(),
        shareUrl: (json['share_url'] ?? '').toString(),
        program: ReferralProgram.fromJson(
          (json['program'] as Map<String, dynamic>?) ?? const {},
        ),
        totals: ReferralTotals.fromJson(
          (json['totals'] as Map<String, dynamic>?) ?? const {},
        ),
        referrals: ((json['referrals'] as List?) ?? const [])
            .whereType<Map<String, dynamic>>()
            .map(ReferralItem.fromJson)
            .toList(),
      );
}

int _asInt(dynamic v) {
  if (v is int) return v;
  return int.tryParse(v?.toString() ?? '') ?? 0;
}

DateTime? _asDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}
