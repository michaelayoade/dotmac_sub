import 'connection_status.dart';
import 'status_presentation.dart';

/// Canonical Account/Service Health contract from GET /me/account-health.
class AccountHealth {
  const AccountHealth({
    required this.accountId,
    required this.displayName,
    required this.lifecycle,
    required this.financial,
    required this.services,
    required this.asOf,
    this.accountNumber,
    this.subscriberNumber,
    this.primaryAction,
  });

  final String accountId;
  final String? accountNumber;
  final String? subscriberNumber;
  final String displayName;
  final StatusPresentation lifecycle;
  final AccountFinancialHealth financial;
  final List<AccountServiceHealth> services;
  final AccountHealthAction? primaryAction;
  final DateTime asOf;

  AccountServiceHealth? forSubscription(String subscriptionId) {
    for (final service in services) {
      if (service.subscriptionId == subscriptionId) return service;
    }
    return null;
  }

  List<AccountServiceHealth> get unavailableServices => services
      .where((service) => service.accessState != 'available')
      .toList(growable: false);

  factory AccountHealth.fromJson(Map<String, dynamic> json) => AccountHealth(
    accountId: json['account_id'].toString(),
    accountNumber: json['account_number'] as String?,
    subscriberNumber: json['subscriber_number'] as String?,
    displayName: json['display_name'] as String? ?? 'Customer',
    lifecycle: StatusPresentation.fromJson(
      (json['lifecycle'] as Map).cast<String, dynamic>(),
    ),
    financial: AccountFinancialHealth.fromJson(
      (json['financial'] as Map).cast<String, dynamic>(),
    ),
    services: ((json['services'] as List?) ?? const [])
        .whereType<Map>()
        .map(
          (item) => AccountServiceHealth.fromJson(item.cast<String, dynamic>()),
        )
        .toList(growable: false),
    primaryAction: json['primary_action'] is Map
        ? AccountHealthAction.fromJson(
            (json['primary_action'] as Map).cast<String, dynamic>(),
          )
        : null,
    asOf: DateTime.parse(json['as_of'].toString()).toLocal(),
  );
}

class AccountFinancialHealth {
  const AccountFinancialHealth({
    required this.billingMode,
    required this.receivables,
    required this.prepaidFunding,
  });

  final AvailableValue<String> billingMode;
  final AvailableValue<List<ReceivableLane>> receivables;
  final AvailableValue<MoneyAmount> prepaidFunding;

  factory AccountFinancialHealth.fromJson(Map<String, dynamic> json) =>
      AccountFinancialHealth(
        billingMode: AvailableValue.fromJson(
          (json['billing_mode'] as Map).cast<String, dynamic>(),
          (value) => value.toString(),
        ),
        receivables: AvailableValue.fromJson(
          (json['receivables'] as Map).cast<String, dynamic>(),
          (value) => (value as List)
              .whereType<Map>()
              .map(
                (lane) => ReceivableLane.fromJson(lane.cast<String, dynamic>()),
              )
              .toList(growable: false),
        ),
        prepaidFunding: AvailableValue.fromJson(
          (json['prepaid_funding'] as Map).cast<String, dynamic>(),
          (value) =>
              MoneyAmount.fromJson((value as Map).cast<String, dynamic>()),
        ),
      );
}

class AvailableValue<T> {
  const AvailableValue({required this.kind, this.value, this.asOf});

  final String kind;
  final T? value;
  final DateTime? asOf;

  bool get isPresent => kind == 'present' || kind == 'stale';
  bool get isStale => kind == 'stale';

  factory AvailableValue.fromJson(
    Map<String, dynamic> json,
    T Function(dynamic value) decode,
  ) {
    final kind = json['kind'] as String? ?? 'unknown';
    final raw = json['value'];
    return AvailableValue(
      kind: kind,
      value: raw == null ? null : decode(raw),
      asOf: _toDate(json['as_of']),
    );
  }
}

class MoneyAmount {
  const MoneyAmount({required this.amount, required this.currency});

  final double amount;
  final String currency;

  factory MoneyAmount.fromJson(Map<String, dynamic> json) => MoneyAmount(
    amount: _toDouble(json['amount']) ?? 0,
    currency: json['currency'] as String? ?? 'NGN',
  );
}

class ReceivableLane {
  const ReceivableLane({
    required this.currency,
    required this.outstanding,
    required this.overdue,
    required this.overdueCount,
  });

  final String currency;
  final double outstanding;
  final double overdue;
  final int overdueCount;

  factory ReceivableLane.fromJson(Map<String, dynamic> json) => ReceivableLane(
    currency: json['currency'] as String? ?? 'NGN',
    outstanding: _toDouble(json['outstanding']) ?? 0,
    overdue: _toDouble(json['overdue']) ?? 0,
    overdueCount: json['overdue_count'] as int? ?? 0,
  );
}

class AccountServiceHealth {
  const AccountServiceHealth({
    required this.subscriptionId,
    required this.offerName,
    required this.lifecycle,
    required this.accessState,
    required this.access,
    required this.accessReason,
    required this.session,
    required this.sessionPresentation,
    required this.connection,
    this.billingMode,
    this.nextChargeAt,
    this.expiresAt,
    this.nextAction,
  });

  final String subscriptionId;
  final String offerName;
  final StatusPresentation lifecycle;
  final String? billingMode;
  final String accessState;
  final StatusPresentation access;
  final String accessReason;
  final AccountHealthSession session;
  final StatusPresentation sessionPresentation;
  final AvailableValue<ConnectionStatus> connection;
  final DateTime? nextChargeAt;
  final DateTime? expiresAt;
  final AccountHealthAction? nextAction;

  bool get usable => accessState == 'available';

  factory AccountServiceHealth.fromJson(Map<String, dynamic> json) =>
      AccountServiceHealth(
        subscriptionId: json['subscription_id'].toString(),
        offerName: json['offer_name'] as String? ?? 'Service',
        lifecycle: StatusPresentation.fromJson(
          (json['lifecycle'] as Map).cast<String, dynamic>(),
        ),
        billingMode: json['billing_mode'] as String?,
        accessState: json['access_state'] as String? ?? 'unavailable',
        access: StatusPresentation.fromJson(
          (json['access'] as Map).cast<String, dynamic>(),
        ),
        accessReason: json['access_reason'] as String? ?? '',
        session: AccountHealthSession.fromJson(
          (json['session'] as Map).cast<String, dynamic>(),
        ),
        sessionPresentation: StatusPresentation.fromJson(
          (json['session_presentation'] as Map).cast<String, dynamic>(),
        ),
        connection: AvailableValue.fromJson(
          (json['connection'] as Map).cast<String, dynamic>(),
          (value) =>
              ConnectionStatus.fromJson((value as Map).cast<String, dynamic>()),
        ),
        nextChargeAt: _toDate(json['next_charge_at']),
        expiresAt: _toDate(json['expires_at']),
        nextAction: json['next_action'] is Map
            ? AccountHealthAction.fromJson(
                (json['next_action'] as Map).cast<String, dynamic>(),
              )
            : null,
      );
}

class AccountHealthSession {
  const AccountHealthSession({
    required this.state,
    required this.binding,
    this.observedAt,
    this.framedIpAddress,
    this.nasDeviceId,
  });

  final String state;
  final String binding;
  final DateTime? observedAt;
  final String? framedIpAddress;
  final String? nasDeviceId;

  bool get isOnline => state == 'connected';

  factory AccountHealthSession.fromJson(Map<String, dynamic> json) =>
      AccountHealthSession(
        state: json['state'] as String? ?? 'offline',
        binding: json['binding'] as String? ?? 'none',
        observedAt: _toDate(json['observed_at']),
        framedIpAddress: json['framed_ip_address'] as String?,
        nasDeviceId: json['nas_device_id']?.toString(),
      );
}

class AccountHealthAction {
  const AccountHealthAction({
    required this.kind,
    required this.label,
    required this.message,
    required this.currency,
    required this.restoresService,
    this.amount,
  });

  final String kind;
  final String label;
  final String message;
  final double? amount;
  final String currency;
  final bool restoresService;

  bool get isFinancial => kind == 'top_up' || kind == 'pay_invoices';

  factory AccountHealthAction.fromJson(Map<String, dynamic> json) =>
      AccountHealthAction(
        kind: json['kind'] as String? ?? 'contact_support',
        label: json['label'] as String? ?? 'Contact support',
        message: json['message'] as String? ?? 'Contact support for help.',
        amount: _toDouble(json['amount']),
        currency: json['currency'] as String? ?? 'NGN',
        restoresService: json['restores_service'] as bool? ?? false,
      );
}

double? _toDouble(dynamic value) {
  if (value == null) return null;
  if (value is num) return value.toDouble();
  return double.tryParse(value.toString());
}

DateTime? _toDate(dynamic value) {
  if (value == null) return null;
  return DateTime.tryParse(value.toString())?.toLocal();
}
