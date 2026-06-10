// Models for the reseller API (app/api/reseller.py, mounted at /api/v1).
import '../core/parsers.dart';

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

  factory ResellerAccount.fromJson(Map<String, dynamic> json) =>
      ResellerAccount(
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

class ResellerSubscriptionRef {
  ResellerSubscriptionRef({
    required this.id,
    required this.offerName,
    required this.status,
    this.startDate,
  });

  final String id;
  final String offerName;
  final String status;
  final DateTime? startDate;

  factory ResellerSubscriptionRef.fromJson(Map<String, dynamic> json) =>
      ResellerSubscriptionRef(
        id: json['id'].toString(),
        offerName: json['offer_name'] as String? ?? 'N/A',
        status: json['status'] as String? ?? 'unknown',
        startDate: json['start_date'] == null
            ? null
            : DateTime.tryParse(json['start_date'].toString()),
      );
}

class ResellerInvoiceSummary {
  ResellerInvoiceSummary({
    required this.id,
    required this.status,
    required this.totalAmount,
    required this.balanceDue,
    this.invoiceNumber,
    this.issuedAt,
    this.dueDate,
  });

  final String id;
  final String status;
  final num totalAmount;
  final num balanceDue;
  final String? invoiceNumber;
  final DateTime? issuedAt;
  final DateTime? dueDate;

  factory ResellerInvoiceSummary.fromJson(Map<String, dynamic> json) =>
      ResellerInvoiceSummary(
        id: json['id'].toString(),
        status: json['status'] as String? ?? 'draft',
        totalAmount: (json['total_amount'] as num?) ?? 0,
        balanceDue: (json['balance_due'] as num?) ?? 0,
        invoiceNumber: json['invoice_number'] as String?,
        issuedAt: json['issued_at'] == null
            ? null
            : DateTime.tryParse(json['issued_at'].toString()),
        dueDate: json['due_date'] == null
            ? null
            : DateTime.tryParse(json['due_date'].toString()),
      );
}

class ResellerAccountDetail {
  ResellerAccountDetail({
    required this.id,
    required this.subscriberName,
    required this.status,
    required this.openBalance,
    required this.subscriptions,
    this.accountNumber,
    this.email,
    this.phone,
  });

  final String id;
  final String subscriberName;
  final String status;
  final num openBalance;
  final List<ResellerSubscriptionRef> subscriptions;
  final String? accountNumber;
  final String? email;
  final String? phone;

  factory ResellerAccountDetail.fromJson(Map<String, dynamic> json) =>
      ResellerAccountDetail(
        id: json['id'].toString(),
        subscriberName: json['subscriber_name'] as String? ?? '',
        status: json['status'] as String? ?? 'active',
        openBalance: (json['open_balance'] as num?) ?? 0,
        subscriptions: (json['subscriptions'] as List? ?? const [])
            .cast<Map<String, dynamic>>()
            .map(ResellerSubscriptionRef.fromJson)
            .toList(),
        accountNumber: json['account_number'] as String?,
        email: json['email'] as String?,
        phone: json['phone'] as String?,
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

/// Mirrors GET /reseller/revenue (reseller_portal.get_revenue_summary).
class ResellerRevenue {
  ResellerRevenue({
    required this.totalPaid,
    required this.totalOutstanding,
    required this.accountCount,
    this.monthly = const [],
  });

  final double totalPaid;
  final double totalOutstanding;
  final int accountCount;
  final List<ResellerRevenueMonth> monthly;

  factory ResellerRevenue.fromJson(Map<String, dynamic> json) =>
      ResellerRevenue(
        totalPaid: asDouble(json['total_paid']),
        totalOutstanding: asDouble(json['total_outstanding']),
        accountCount: (json['account_count'] as num?)?.toInt() ?? 0,
        monthly: (json['monthly'] as List? ?? const [])
            .cast<Map<String, dynamic>>()
            .map(ResellerRevenueMonth.fromJson)
            .toList(),
      );
}

/// One month of paid revenue.
class ResellerRevenueMonth {
  ResellerRevenueMonth({
    required this.year,
    required this.month,
    required this.total,
    required this.count,
  });

  final int year;
  final int month;
  final double total;
  final int count;

  String get label {
    const names = [
      'Jan',
      'Feb',
      'Mar',
      'Apr',
      'May',
      'Jun',
      'Jul',
      'Aug',
      'Sep',
      'Oct',
      'Nov',
      'Dec',
    ];
    final m = (month >= 1 && month <= 12) ? names[month - 1] : '$month';
    return "$m '${year % 100}";
  }

  factory ResellerRevenueMonth.fromJson(Map<String, dynamic> json) =>
      ResellerRevenueMonth(
        year: (json['year'] as num?)?.toInt() ?? 0,
        month: (json['month'] as num?)?.toInt() ?? 0,
        total: asDouble(json['total']),
        count: (json['count'] as num?)?.toInt() ?? 0,
      );
}
