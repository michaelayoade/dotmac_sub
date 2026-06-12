// Top-up (prepaid account credit) models mirroring app/api/me.py topup endpoints.

import '../core/parsers.dart';

class TopupPage {
  TopupPage({
    required this.providerType,
    required this.currency,
    required this.minAmount,
    required this.maxAmount,
    this.providerPublicKey,
    this.prepaidBalance,
    this.presetAmounts = const [],
    this.customerEmail,
  });

  final String providerType;
  final String currency;
  final int minAmount;
  final int maxAmount;
  final String? providerPublicKey;
  final double? prepaidBalance;
  final List<int> presetAmounts;
  final String? customerEmail;

  factory TopupPage.fromJson(Map<String, dynamic> json) => TopupPage(
    providerType: json['provider_type'] as String? ?? 'paystack',
    currency: json['currency'] as String? ?? 'NGN',
    minAmount: (json['min_amount'] as num?)?.toInt() ?? 1000,
    maxAmount: (json['max_amount'] as num?)?.toInt() ?? 500000,
    providerPublicKey: json['provider_public_key'] as String?,
    prepaidBalance: asDoubleOrNull(json['prepaid_balance']),
    presetAmounts: (json['preset_amounts'] as List? ?? const [])
        .map((e) => (e as num).toInt())
        .toList(),
    customerEmail: json['customer_email'] as String?,
  );
}

class TopupInitiation {
  TopupInitiation({
    required this.intentId,
    required this.providerType,
    required this.paymentReference,
    required this.amount,
    required this.currency,
    this.providerPublicKey,
    this.customerEmail,
  });

  final String intentId;
  final String providerType;
  final String paymentReference;
  final double amount;
  final String currency;
  final String? providerPublicKey;
  final String? customerEmail;

  factory TopupInitiation.fromJson(Map<String, dynamic> json) =>
      TopupInitiation(
        intentId: json['intent_id'].toString(),
        providerType: json['provider_type'] as String? ?? 'paystack',
        paymentReference: json['payment_reference'].toString(),
        amount: asDouble(json['amount']),
        currency: json['currency'] as String? ?? 'NGN',
        providerPublicKey: json['provider_public_key'] as String?,
        customerEmail: json['customer_email'] as String?,
      );
}

class TopupResult {
  TopupResult({
    required this.reference,
    required this.amount,
    this.alreadyRecorded = false,
    this.availableBalance,
    this.creditAdded,
  });

  final String reference;
  final double amount;
  final bool alreadyRecorded;
  final double? availableBalance;
  final double? creditAdded;

  factory TopupResult.fromJson(Map<String, dynamic> json) => TopupResult(
    reference: json['reference'].toString(),
    amount: asDouble(json['amount']),
    alreadyRecorded: json['already_recorded'] as bool? ?? false,
    availableBalance: asDoubleOrNull(json['available_balance']),
    creditAdded: asDoubleOrNull(json['credit_added']),
  );
}
