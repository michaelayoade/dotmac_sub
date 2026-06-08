// Models for the reseller API (app/api/reseller.py, mounted at /api/v1).

class ResellerAccount {
  ResellerAccount({
    required this.id,
    required this.subscriberName,
    required this.status,
    required this.openBalance,
    required this.openInvoices,
    this.accountNumber,
    this.lastPaymentAt,
  });

  final String id;
  final String subscriberName;
  final String status;
  final num openBalance;
  final int openInvoices;
  final String? accountNumber;
  final DateTime? lastPaymentAt;

  factory ResellerAccount.fromJson(Map<String, dynamic> json) => ResellerAccount(
        id: json['id'].toString(),
        subscriberName: json['subscriber_name'] as String? ?? '',
        status: json['status'] as String? ?? 'active',
        openBalance: (json['open_balance'] as num?) ?? 0,
        openInvoices: (json['open_invoices'] as num?)?.toInt() ?? 0,
        accountNumber: json['account_number'] as String?,
        lastPaymentAt: json['last_payment_at'] == null
            ? null
            : DateTime.tryParse(json['last_payment_at'].toString()),
      );
}

class ResellerTotals {
  ResellerTotals({
    required this.accounts,
    required this.openBalance,
    required this.openInvoices,
  });

  final int accounts;
  final num openBalance;
  final int openInvoices;

  factory ResellerTotals.fromJson(Map<String, dynamic> json) => ResellerTotals(
        accounts: (json['accounts'] as num?)?.toInt() ?? 0,
        openBalance: (json['open_balance'] as num?) ?? 0,
        openInvoices: (json['open_invoices'] as num?)?.toInt() ?? 0,
      );
}

class ResellerAlert {
  ResellerAlert({required this.level, required this.message, this.actionUrl});

  final String level;
  final String message;
  final String? actionUrl;

  factory ResellerAlert.fromJson(Map<String, dynamic> json) => ResellerAlert(
        level: json['level'] as String? ?? 'info',
        message: json['message'] as String? ?? '',
        actionUrl: json['action_url'] as String?,
      );
}

class ResellerDashboard {
  ResellerDashboard({
    required this.accounts,
    required this.totals,
    required this.alerts,
  });

  final List<ResellerAccount> accounts;
  final ResellerTotals totals;
  final List<ResellerAlert> alerts;

  factory ResellerDashboard.fromJson(Map<String, dynamic> json) =>
      ResellerDashboard(
        accounts: (json['accounts'] as List? ?? const [])
            .cast<Map<String, dynamic>>()
            .map(ResellerAccount.fromJson)
            .toList(),
        totals: ResellerTotals.fromJson(
            (json['totals'] as Map<String, dynamic>?) ?? const {}),
        alerts: (json['alerts'] as List? ?? const [])
            .cast<Map<String, dynamic>>()
            .map(ResellerAlert.fromJson)
            .toList(),
      );
}
