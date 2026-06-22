/// Mirrors ServiceStatusResponse from app/schemas/service_status.py.
///
/// The truthful "is my service good, and when does it lapse" view. Expiry is
/// status/balance-driven, not date-driven: prepaid lapses on balance exhaustion
/// (balance + grace/deactivation), postpaid only via dunning on overdue
/// invoices. `nextChargeAt` is the next charge/invoice date, never an expiry.
class ServiceStatus {
  ServiceStatus({
    required this.billingMode,
    required this.currency,
    required this.lowBalance,
    required this.inDunning,
    required this.services,
    this.balance,
    this.minBalance,
    this.graceUntil,
    this.deactivationAt,
    this.outstanding,
    this.oldestOverdueDueAt,
  });

  final String billingMode; // prepaid | postpaid
  final String currency;

  // Prepaid health (null for postpaid).
  final double? balance;
  final double? minBalance;
  final bool lowBalance;
  final DateTime? graceUntil;
  final DateTime? deactivationAt;

  // Postpaid health (null/false for prepaid).
  final double? outstanding;
  final DateTime? oldestOverdueDueAt;
  final bool inDunning;

  final List<ServiceStatusItem> services;

  bool get isPrepaid => billingMode == 'prepaid';

  /// An active service is heading for a cut and the customer can prevent it by
  /// paying: prepaid low balance, or postpaid overdue. Drives the renew banner.
  bool get needsRenewal => services.any((s) => s.usable && s.actionable);

  factory ServiceStatus.fromJson(Map<String, dynamic> json) {
    final list = (json['services'] as List?) ?? const [];
    return ServiceStatus(
      billingMode: json['billing_mode'] as String? ?? 'prepaid',
      currency: json['currency'] as String? ?? 'NGN',
      balance: _toDouble(json['balance']),
      minBalance: _toDouble(json['min_balance']),
      lowBalance: json['low_balance'] as bool? ?? false,
      graceUntil: _toDate(json['grace_until']),
      deactivationAt: _toDate(json['deactivation_at']),
      outstanding: _toDouble(json['outstanding']),
      oldestOverdueDueAt: _toDate(json['oldest_overdue_due_at']),
      inDunning: json['in_dunning'] as bool? ?? false,
      services: list
          .whereType<Map>()
          .map((e) => ServiceStatusItem.fromJson(e.cast<String, dynamic>()))
          .toList(),
    );
  }
}

class ServiceStatusItem {
  ServiceStatusItem({
    required this.subscriptionId,
    required this.status,
    required this.billingMode,
    required this.usable,
    required this.reason,
    this.offerName,
    this.expiresAt,
    this.nextChargeAt,
  });

  final String subscriptionId;
  final String? offerName;
  final String status;
  final String billingMode;
  final bool usable;
  final DateTime? expiresAt;
  final DateTime? nextChargeAt;

  /// ok | low_balance | overdue | needs_payment | stopped | ended
  final String reason;

  /// A running service the customer can keep alive by paying now.
  bool get actionable => reason == 'low_balance' || reason == 'overdue';

  factory ServiceStatusItem.fromJson(Map<String, dynamic> json) =>
      ServiceStatusItem(
        subscriptionId: json['subscription_id'].toString(),
        offerName: json['offer_name'] as String?,
        status: json['status'] as String? ?? 'active',
        billingMode: json['billing_mode'] as String? ?? 'prepaid',
        usable: json['usable'] as bool? ?? false,
        expiresAt: _toDate(json['expires_at']),
        nextChargeAt: _toDate(json['next_charge_at']),
        reason: json['reason'] as String? ?? 'ok',
      );
}

double? _toDouble(dynamic v) {
  if (v == null) return null;
  if (v is num) return v.toDouble();
  return double.tryParse(v.toString());
}

DateTime? _toDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}
