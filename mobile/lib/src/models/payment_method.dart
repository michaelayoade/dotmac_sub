/// Autopay status (GET /me/autopay).
class AutopayStatus {
  AutopayStatus({required this.enabled, this.paymentMethodId});

  final bool enabled;
  final String? paymentMethodId;

  factory AutopayStatus.fromJson(Map<String, dynamic> json) => AutopayStatus(
    enabled: json['enabled'] as bool? ?? false,
    paymentMethodId: json['payment_method_id']?.toString(),
  );
}

/// A saved card (GET /me/payment-methods). The reusable token is never sent to
/// the client — only display fields.
class SavedCard {
  SavedCard({
    required this.id,
    required this.methodType,
    this.label,
    this.last4,
    this.brand,
    this.expiresMonth,
    this.expiresYear,
    this.isDefault = false,
  });

  final String id;
  final String methodType;
  final String? label;
  final String? last4;
  final String? brand;
  final int? expiresMonth;
  final int? expiresYear;
  final bool isDefault;

  String get title {
    final l = label?.trim();
    if (l != null && l.isNotEmpty) return l;
    final b = (brand ?? 'Card').trim();
    return last4 != null ? '$b •••• $last4' : b;
  }

  /// "08/30" style expiry, or null when unknown.
  String? get expiry {
    if (expiresMonth == null || expiresYear == null) return null;
    final mm = expiresMonth!.toString().padLeft(2, '0');
    final yy = (expiresYear! % 100).toString().padLeft(2, '0');
    return '$mm/$yy';
  }

  factory SavedCard.fromJson(Map<String, dynamic> json) => SavedCard(
    id: json['id'].toString(),
    methodType: json['method_type'] as String? ?? 'card',
    label: json['label'] as String?,
    last4: json['last4'] as String?,
    brand: json['brand'] as String?,
    expiresMonth: (json['expires_month'] as num?)?.toInt(),
    expiresYear: (json['expires_year'] as num?)?.toInt(),
    isDefault: json['is_default'] as bool? ?? false,
  );
}
