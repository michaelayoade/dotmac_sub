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
    this.primaryAction,
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
  final ServiceStatusAction? primaryAction;

  bool get isPrepaid => billingMode == 'prepaid';

  /// An active service is heading for a cut and the server says a financial
  /// action can prevent it. The client never derives this from subscription
  /// status or invoice rows.
  bool get needsRenewal =>
      services.any((s) => s.usable && (s.action?.isFinancial ?? false));

  ServiceStatusItem? forSubscription(String subscriptionId) {
    for (final service in services) {
      if (service.subscriptionId == subscriptionId) return service;
    }
    return null;
  }

  List<ServiceStatusItem> get unavailableServices => services
      .where((s) =>
          !s.usable &&
          const {'blocked', 'suspended', 'stopped'}.contains(s.status))
      .toList(growable: false);

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
      primaryAction: json['primary_action'] is Map
          ? ServiceStatusAction.fromJson(
              (json['primary_action'] as Map).cast<String, dynamic>())
          : null,
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
    this.action,
  });

  final String subscriptionId;
  final String? offerName;
  final String status;
  final String billingMode;
  final bool usable;
  final DateTime? expiresAt;
  final DateTime? nextChargeAt;

  /// Server-owned service reason; clients do not infer actions from this text.
  final String reason;
  final ServiceStatusAction? action;

  bool get actionable => action != null;

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
        action: json['action'] is Map
            ? ServiceStatusAction.fromJson(
                (json['action'] as Map).cast<String, dynamic>())
            : null,
      );
}

class ServiceStatusAction {
  ServiceStatusAction({
    required this.kind,
    required this.label,
    required this.message,
    required this.currency,
    required this.restoresService,
    this.amount,
  });

  /// top_up | pay_invoices | view_usage | contact_support
  final String kind;
  final String label;
  final String message;
  final double? amount;
  final String currency;

  /// True only when the server has proven this action clears every known hold.
  final bool restoresService;

  bool get isFinancial => kind == 'top_up' || kind == 'pay_invoices';

  factory ServiceStatusAction.fromJson(Map<String, dynamic> json) =>
      ServiceStatusAction(
        kind: json['kind'] as String? ?? 'contact_support',
        label: json['label'] as String? ?? 'Contact support',
        message: json['message'] as String? ?? 'Contact support for help.',
        amount: _toDouble(json['amount']),
        currency: json['currency'] as String? ?? 'NGN',
        restoresService: json['restores_service'] as bool? ?? false,
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
