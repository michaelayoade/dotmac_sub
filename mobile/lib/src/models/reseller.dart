// Models for the reseller API (app/api/reseller.py, mounted at /api/v1).

/// Money fields come back as JSON **strings** (serialized `Decimal`, e.g.
/// "749012363.52"); counts come back as numbers. Parse defensively so a String
/// value never throws a `num` type-cast at runtime.
num _toNum(dynamic v) {
  if (v is num) return v;
  if (v is String) return num.tryParse(v) ?? 0;
  return 0;
}

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
        openBalance: _toNum(json['open_balance']),
        openInvoices: _toNum(json['open_invoices']).toInt(),
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
        totalAmount: _toNum(json['total_amount']),
        balanceDue: _toNum(json['balance_due']),
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
        openBalance: _toNum(json['open_balance']),
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
        accounts: _toNum(json['accounts']).toInt(),
        openBalance: _toNum(json['open_balance']),
        openInvoices: _toNum(json['open_invoices']).toInt(),
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
    this.openTickets = 0,
  });

  final List<ResellerAccount> accounts;
  final ResellerTotals totals;
  final List<ResellerAlert> alerts;

  /// Open CRM tickets across the page's accounts (0 when CRM unreachable).
  final int openTickets;

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
        openTickets: (json['open_tickets'] as num?)?.toInt() ?? 0,
      );
}

/// One CRM support ticket on a managed account
/// (GET /reseller/accounts/{id}/tickets).
class ResellerTicket {
  ResellerTicket({
    required this.id,
    required this.subject,
    this.status,
    this.priority,
    this.createdAt,
  });

  final String id;
  final String subject;
  final String? status;
  final String? priority;
  final DateTime? createdAt;

  bool get isOpen =>
      status == 'open' ||
      status == 'in_progress' ||
      status == 'waiting_on_agent';

  factory ResellerTicket.fromJson(Map<String, dynamic> json) => ResellerTicket(
        id: json['id'].toString(),
        subject: json['subject'] as String? ?? 'Ticket',
        status: json['status'] as String?,
        priority: json['priority'] as String?,
        createdAt: json['created_at'] == null
            ? null
            : DateTime.tryParse(json['created_at'].toString())?.toLocal(),
      );
}

/// Tickets payload with the CRM-availability soft-failure flag.
class ResellerTicketsPage {
  ResellerTicketsPage({required this.items, required this.crmAvailable});

  final List<ResellerTicket> items;
  final bool crmAvailable;

  factory ResellerTicketsPage.fromJson(Map<String, dynamic> json) =>
      ResellerTicketsPage(
        items: (json['items'] as List? ?? const [])
            .cast<Map<String, dynamic>>()
            .map(ResellerTicket.fromJson)
            .toList(),
        crmAvailable: json['crm_available'] as bool? ?? true,
      );
}
