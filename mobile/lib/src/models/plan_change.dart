// Plan-change models mirroring app/api/me.py plan-change endpoints.

class PlanOffer {
  PlanOffer({
    required this.id,
    required this.name,
    required this.amount,
    required this.currency,
    required this.periodLabel,
  });

  final String id;
  final String name;
  final double amount;
  final String currency;
  final String periodLabel;

  factory PlanOffer.fromJson(Map<String, dynamic> json) => PlanOffer(
        id: json['id'].toString(),
        name: json['name'] as String? ?? 'Plan',
        amount: (json['amount'] as num?)?.toDouble() ?? 0,
        currency: json['currency'] as String? ?? 'NGN',
        periodLabel: json['period_label'] as String? ?? '/cycle',
      );
}

class PlanChangeOptions {
  PlanChangeOptions({
    this.currentOffer,
    this.availableOffers = const [],
    this.walletBalance,
    this.nextBillingDate,
    this.billingMessage,
  });

  final PlanOffer? currentOffer;
  final List<PlanOffer> availableOffers;
  final double? walletBalance;
  final DateTime? nextBillingDate;
  final String? billingMessage;

  factory PlanChangeOptions.fromJson(Map<String, dynamic> json) {
    final current = json['current_offer'];
    return PlanChangeOptions(
      currentOffer: current is Map ? PlanOffer.fromJson(current.cast()) : null,
      availableOffers: (json['available_offers'] as List? ?? const [])
          .cast<Map<String, dynamic>>()
          .map(PlanOffer.fromJson)
          .toList(),
      walletBalance: _toDouble(json['wallet_balance']),
      nextBillingDate:
          DateTime.tryParse(json['next_billing_date']?.toString() ?? '')
              ?.toLocal(),
      billingMessage: json['billing_message'] as String?,
    );
  }
}

/// Prorated quote for switching to a target offer (prepaid). Empty/no-op for
/// postpaid plans, where the new rate simply applies from the next invoice.
class PlanChangeQuote {
  PlanChangeQuote({
    required this.hasProration,
    this.chargeAmount = 0,
    this.netAmount = 0,
    this.currentBalance = 0,
    this.shortfall = 0,
    this.daysRemaining = 0,
    this.canApplyImmediately = false,
    this.isUpgrade = false,
    this.isDowngrade = false,
  });

  final bool hasProration;
  final double chargeAmount;
  final double netAmount; // amount payable now (after wallet credit)
  final double currentBalance;
  final double shortfall; // > 0 means top-up needed
  final int daysRemaining;
  final bool canApplyImmediately;
  final bool isUpgrade;
  final bool isDowngrade;

  bool get needsTopUp => shortfall > 0;

  factory PlanChangeQuote.fromJson(Map<String, dynamic> json) {
    if (json.isEmpty || !json.containsKey('net_amount')) {
      return PlanChangeQuote(hasProration: false);
    }
    return PlanChangeQuote(
      hasProration: true,
      chargeAmount: _toDouble(json['charge_amount']) ?? 0,
      netAmount: _toDouble(json['net_amount']) ?? 0,
      currentBalance: _toDouble(json['current_balance']) ?? 0,
      shortfall: _toDouble(json['shortfall']) ?? 0,
      daysRemaining: (json['days_remaining'] as num?)?.toInt() ?? 0,
      canApplyImmediately: json['can_apply_immediately'] as bool? ?? false,
      isUpgrade: json['is_upgrade'] as bool? ?? false,
      isDowngrade: json['is_downgrade'] as bool? ?? false,
    );
  }
}

double? _toDouble(dynamic v) {
  if (v == null) return null;
  if (v is num) return v.toDouble();
  return double.tryParse(v.toString());
}
