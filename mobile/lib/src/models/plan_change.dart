// Plan-change models mirroring app/api/me.py plan-change endpoints.

import '../core/parsers.dart';

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
        amount: asDouble(json['amount']),
        currency: json['currency'] as String? ?? 'NGN',
        periodLabel: json['period_label'] as String? ?? '/cycle',
      );
}

class PlanChangeOptions {
  PlanChangeOptions({
    this.currentOffer,
    this.availableOffers = const [],
    this.prepaidFunding,
    this.postpaidReceivables = 0,
    this.collectionBlockingBalance = 0,
    this.nextBillingDate,
    this.billingMessage,
  });

  final PlanOffer? currentOffer;
  final List<PlanOffer> availableOffers;
  final double? prepaidFunding;
  final double postpaidReceivables;
  final double collectionBlockingBalance;
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
      prepaidFunding: asDoubleOrNull(json['prepaid_funding']),
      postpaidReceivables: asDouble(json['postpaid_receivables']),
      collectionBlockingBalance: asDouble(json['collection_blocking_balance']),
      nextBillingDate:
          DateTime.tryParse(json['next_billing_date']?.toString() ?? '')
              ?.toLocal(),
      billingMessage: json['billing_message'] as String?,
    );
  }
}

/// Owner preview for switching to a target offer. Postpaid and zero-money
/// changes carry a fingerprint with an explicit no-ledger result.
class PlanChangeQuote {
  PlanChangeQuote({
    required this.hasProration,
    this.chargeAmount = 0,
    this.netAmount = 0,
    this.prepaidFundingBefore = 0,
    this.prepaidFundingAfter = 0,
    this.postpaidReceivables = 0,
    this.collectionBlockingBalance = 0,
    this.shortfall = 0,
    this.daysRemaining = 0,
    this.canApplyImmediately = false,
    this.isUpgrade = false,
    this.isDowngrade = false,
    this.previewFingerprint = '',
    this.hasFinancialEffect = false,
    this.ledgerEntryType,
    this.ledgerSource,
    this.ledgerAmount = 0,
    this.accessConsequence = 'none_plan_change_only',
  });

  final bool hasProration;
  final double chargeAmount;
  final double netAmount;
  final double prepaidFundingBefore;
  final double prepaidFundingAfter;
  final double postpaidReceivables;
  final double collectionBlockingBalance;
  final double shortfall; // > 0 means top-up needed
  final int daysRemaining;
  final bool canApplyImmediately;
  final bool isUpgrade;
  final bool isDowngrade;
  final String previewFingerprint;
  final bool hasFinancialEffect;
  final String? ledgerEntryType;
  final String? ledgerSource;
  final double ledgerAmount;
  final String accessConsequence;

  bool get needsTopUp => shortfall > 0;

  factory PlanChangeQuote.fromJson(Map<String, dynamic> json) {
    return PlanChangeQuote(
      hasProration: json['has_financial_effect'] as bool? ?? false,
      chargeAmount: asDouble(json['charge_amount']),
      netAmount: asDouble(json['net_amount']),
      prepaidFundingBefore: asDouble(json['prepaid_funding_before']),
      prepaidFundingAfter: asDouble(json['prepaid_funding_after']),
      postpaidReceivables: asDouble(json['postpaid_receivables']),
      collectionBlockingBalance: asDouble(json['collection_blocking_balance']),
      shortfall: asDouble(json['shortfall']),
      daysRemaining: (json['days_remaining'] as num?)?.toInt() ?? 0,
      canApplyImmediately: json['can_apply_immediately'] as bool? ?? false,
      isUpgrade: json['is_upgrade'] as bool? ?? false,
      isDowngrade: json['is_downgrade'] as bool? ?? false,
      previewFingerprint: json['preview_fingerprint'] as String? ?? '',
      hasFinancialEffect: json['has_financial_effect'] as bool? ?? false,
      ledgerEntryType: json['ledger_entry_type'] as String?,
      ledgerSource: json['ledger_source'] as String?,
      ledgerAmount: asDouble(json['ledger_amount']),
      accessConsequence:
          json['access_consequence'] as String? ?? 'none_plan_change_only',
    );
  }
}
