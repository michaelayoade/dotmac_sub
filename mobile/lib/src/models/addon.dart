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
  });

  final String id;
  final String name;
  final int quantity;

  factory ActiveAddon.fromJson(Map<String, dynamic> json) => ActiveAddon(
        id: json['id'].toString(),
        name: json['name'] as String? ?? 'Add-on',
        quantity: (json['quantity'] as num?)?.toInt() ?? 1,
      );
}

class AddonsAvailable {
  AddonsAvailable({
    this.available = const [],
    this.active = const [],
    this.walletBalance,
    this.currency = 'NGN',
  });

  final List<AddonOption> available;
  final List<ActiveAddon> active;
  final double? walletBalance;
  final String currency;

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
        walletBalance: json['wallet_balance'] == null
            ? null
            : asDouble(json['wallet_balance']),
        currency: json['currency'] as String? ?? 'NGN',
      );
}

class AddonQuote {
  AddonQuote({
    required this.charge,
    required this.currency,
    required this.currentBalance,
    required this.shortfall,
    required this.canAfford,
  });

  final double charge;
  final String currency;
  final double currentBalance;
  final double shortfall;
  final bool canAfford;

  factory AddonQuote.fromJson(Map<String, dynamic> json) => AddonQuote(
        charge: asDouble(json['charge']),
        currency: json['currency'] as String? ?? 'NGN',
        currentBalance: asDouble(json['current_balance']),
        shortfall: asDouble(json['shortfall']),
        canAfford: json['can_afford'] as bool? ?? false,
      );
}

class AddonPurchaseResult {
  AddonPurchaseResult({
    required this.success,
    this.reason,
    this.charge,
    this.currency = 'NGN',
    this.newBalance,
    this.shortfall,
  });

  final bool success;
  final String? reason;
  final double? charge;
  final String currency;
  final double? newBalance;
  final double? shortfall;

  bool get insufficient => reason == 'insufficient_balance';

  factory AddonPurchaseResult.fromJson(Map<String, dynamic> json) =>
      AddonPurchaseResult(
        success: json['success'] as bool? ?? false,
        reason: json['reason'] as String?,
        charge: json['charge'] == null ? null : asDouble(json['charge']),
        currency: json['currency'] as String? ?? 'NGN',
        newBalance:
            json['new_balance'] == null ? null : asDouble(json['new_balance']),
        shortfall:
            json['shortfall'] == null ? null : asDouble(json['shortfall']),
      );
}
