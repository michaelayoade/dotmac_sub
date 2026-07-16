// Add-on models (app/api/me.py add-on endpoints).

import '../core/parsers.dart';

class AddonOption {
  AddonOption({
    required this.addOnId,
    required this.name,
    required this.addonType,
    required this.amount,
    required this.currency,
    required this.minQuantity,
    required this.maxQuantity,
    this.description,
    this.grantGb,
  });

  final String addOnId;
  final String name;
  final String addonType;
  final double amount;
  final String currency;
  final int minQuantity;
  final int? maxQuantity;
  final String? description;

  /// GB this add-on grants to the quota bucket — set only for data top-ups.
  final int? grantGb;

  bool get isDataTopup => grantGb != null && grantGb! > 0;

  factory AddonOption.fromJson(Map<String, dynamic> json) => AddonOption(
        addOnId: json['add_on_id'].toString(),
        name: json['name'] as String? ?? 'Add-on',
        addonType: json['addon_type'] as String? ?? 'custom',
        amount: asDouble(json['amount']),
        currency: json['currency'] as String? ?? 'NGN',
        minQuantity: (json['min_quantity'] as num?)?.toInt() ?? 1,
        maxQuantity: (json['max_quantity'] as num?)?.toInt(),
        description: json['description'] as String?,
        grantGb: (json['grant_gb'] as num?)?.toInt(),
      );
}

class ActiveAddon {
  ActiveAddon({
    required this.id,
    required this.name,
    required this.quantity,
    this.addonType,
    this.grantGb,
    this.totalGrantGb,
    this.startsAt,
    this.expiresAt,
    this.validityDays,
    this.isExpired = false,
  });

  final String id;
  final String name;
  final int quantity;
  final String? addonType;

  /// GB granted per unit — set only for data bundles.
  final int? grantGb;

  /// GB granted across the whole purchase (grantGb × quantity).
  final int? totalGrantGb;
  final DateTime? startsAt;

  /// Null = the bundle lasts until the end of the billing period.
  final DateTime? expiresAt;
  final int? validityDays;
  final bool isExpired;

  bool get isDataBundle => grantGb != null && grantGb! > 0;

  /// Days until the bundle lapses; null when it tracks the billing period.
  int? get daysLeft {
    final exp = expiresAt;
    if (exp == null) return null;
    final d = exp.difference(DateTime.now()).inDays;
    return d < 0 ? 0 : d;
  }

  factory ActiveAddon.fromJson(Map<String, dynamic> json) => ActiveAddon(
        id: json['id'].toString(),
        name: json['name'] as String? ?? 'Add-on',
        quantity: (json['quantity'] as num?)?.toInt() ?? 1,
        addonType: json['addon_type'] as String?,
        grantGb: (json['grant_gb'] as num?)?.toInt(),
        totalGrantGb: (json['total_grant_gb'] as num?)?.toInt(),
        startsAt: json['starts_at'] == null
            ? null
            : DateTime.tryParse(json['starts_at'].toString())?.toLocal(),
        expiresAt: json['expires_at'] == null
            ? null
            : DateTime.tryParse(json['expires_at'].toString())?.toLocal(),
        validityDays: (json['validity_days'] as num?)?.toInt(),
        isExpired: json['is_expired'] as bool? ?? false,
      );
}

class AddonsAvailable {
  AddonsAvailable({
    this.available = const [],
    this.active = const [],
  });

  final List<AddonOption> available;
  final List<ActiveAddon> active;

  factory AddonsAvailable.fromJson(Map<String, dynamic> json) =>
      AddonsAvailable(
        available: (json['available'] as List? ?? const [])
            .cast<Map<String, dynamic>>()
            .map(AddonOption.fromJson)
            .toList(),
        active: (json['active'] as List? ?? const [])
            .cast<Map<String, dynamic>>()
            .map(ActiveAddon.fromJson)
            .toList(),
      );
}

class AddonQuote {
  AddonQuote({
    required this.charge,
    required this.currency,
    required this.prepaidFundingBefore,
    required this.prepaidFundingAfter,
    required this.postpaidReceivables,
    required this.shortfall,
    required this.canAfford,
    required this.allowed,
    required this.previewFingerprint,
    this.rejectionReason,
  });

  final double charge;
  final String currency;
  final double prepaidFundingBefore;
  final double prepaidFundingAfter;
  final double postpaidReceivables;
  final double shortfall;
  final bool canAfford;
  final bool allowed;
  final String previewFingerprint;
  final String? rejectionReason;

  factory AddonQuote.fromJson(Map<String, dynamic> json) => AddonQuote(
        charge: asDouble(json['charge']),
        currency: json['currency'] as String? ?? 'NGN',
        prepaidFundingBefore: asDouble(json['prepaid_funding_before']),
        prepaidFundingAfter: asDouble(json['prepaid_funding_after']),
        postpaidReceivables: asDouble(json['postpaid_receivables']),
        shortfall: asDouble(json['shortfall']),
        canAfford: json['can_afford'] as bool? ?? false,
        allowed: json['allowed'] as bool? ?? false,
        previewFingerprint: json['preview_fingerprint'] as String? ?? '',
        rejectionReason: json['rejection_reason'] as String?,
      );
}

class AddonPurchaseResult {
  AddonPurchaseResult({
    required this.success,
    this.reason,
    this.charge,
    this.currency = 'NGN',
    this.prepaidFundingAfter,
    this.shortfall,
  });

  final bool success;
  final String? reason;
  final double? charge;
  final String currency;
  final double? prepaidFundingAfter;
  final double? shortfall;

  bool get insufficient => reason == 'insufficient_prepaid_funding';
  bool get serviceNotActive => reason == 'subscription_not_active';

  factory AddonPurchaseResult.fromJson(Map<String, dynamic> json) =>
      AddonPurchaseResult(
        success: json['success'] as bool? ?? false,
        reason: json['reason'] as String?,
        charge: json['charge'] == null ? null : asDouble(json['charge']),
        currency: json['currency'] as String? ?? 'NGN',
        prepaidFundingAfter: json['prepaid_funding_after'] == null
            ? null
            : asDouble(json['prepaid_funding_after']),
        shortfall:
            json['shortfall'] == null ? null : asDouble(json['shortfall']),
      );
}
