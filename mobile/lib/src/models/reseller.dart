// Models for the reseller API (app/api/reseller.py, mounted at /api/v1).
import '../core/parsers.dart';

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

/// Reseller organization profile + MFA state (GET/PATCH /reseller/profile).
class ResellerProfile {
  ResellerProfile({
    required this.name,
    this.code,
    this.contactEmail,
    this.contactPhone,
    this.notes,
    this.mfaEnabled = false,
    this.mfaMethods = const [],
  });

  final String name;
  final String? code;
  final String? contactEmail;
  final String? contactPhone;
  final String? notes;
  final bool mfaEnabled;
  final List<ResellerMfaMethod> mfaMethods;

  factory ResellerProfile.fromJson(Map<String, dynamic> json) =>
      ResellerProfile(
        name: json['name'] as String? ?? 'Reseller',
        code: json['code'] as String?,
        contactEmail: json['contact_email'] as String?,
        contactPhone: json['contact_phone'] as String?,
        notes: json['notes'] as String?,
        mfaEnabled: json['mfa_enabled'] as bool? ?? false,
        mfaMethods: (json['mfa_methods'] as List? ?? const [])
            .cast<Map<String, dynamic>>()
            .map(ResellerMfaMethod.fromJson)
            .toList(),
      );
}

class ResellerMfaMethod {
  ResellerMfaMethod({required this.id, this.label, this.verified = false});

  final String id;
  final String? label;
  final bool verified;

  factory ResellerMfaMethod.fromJson(Map<String, dynamic> json) =>
      ResellerMfaMethod(
        id: json['id'].toString(),
        label: json['label'] as String?,
        verified: json['verified_at'] != null,
      );
}

/// TOTP enrollment material (POST /reseller/profile/mfa/setup).
class ResellerMfaSetup {
  ResellerMfaSetup({
    required this.methodId,
    required this.secret,
    required this.otpauthUri,
  });

  final String methodId;
  final String secret;
  final String otpauthUri;

  factory ResellerMfaSetup.fromJson(Map<String, dynamic> json) =>
      ResellerMfaSetup(
        methodId: json['method_id'].toString(),
        secret: json['secret'] as String? ?? '',
        otpauthUri: json['otpauth_uri'] as String? ?? '',
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

/// Consolidated billing statement (GET /reseller/billing).
class ResellerBillingSummary {
  ResellerBillingSummary({
    required this.totalOutstanding,
    required this.unallocatedBalance,
    this.recentPayments = const [],
  });

  final double totalOutstanding;
  final double unallocatedBalance;
  final List<ResellerPaymentSummary> recentPayments;

  factory ResellerBillingSummary.fromJson(Map<String, dynamic> json) =>
      ResellerBillingSummary(
        totalOutstanding: asDouble(json['total_outstanding']),
        unallocatedBalance: asDouble(json['unallocated_balance']),
        recentPayments: (json['recent_payments'] as List? ?? const [])
            .cast<Map<String, dynamic>>()
            .map(ResellerPaymentSummary.fromJson)
            .toList(),
      );
}

class ResellerPaymentSummary {
  ResellerPaymentSummary({
    required this.amount,
    this.currency = 'NGN',
    this.method,
    this.receivedAt,
  });

  final double amount;
  final String currency;
  final String? method;
  final DateTime? receivedAt;

  factory ResellerPaymentSummary.fromJson(Map<String, dynamic> json) =>
      ResellerPaymentSummary(
        amount: asDouble(json['amount']),
        currency: json['currency'] as String? ?? 'NGN',
        method: (json['method'] ?? json['payment_method'])?.toString(),
        receivedAt: json['received_at'] == null
            ? null
            : DateTime.tryParse(json['received_at'].toString())?.toLocal(),
      );
}

/// Gateway checkout context (POST /reseller/billing/pay/intent).
class ResellerPayIntent {
  ResellerPayIntent({
    required this.providerType,
    required this.reference,
    required this.amount,
    required this.currency,
    this.publicKey,
    this.metadata = const {},
  });

  final String providerType;
  final String reference;
  final double amount;
  final String currency;
  final String? publicKey;
  final Map<String, String> metadata;

  factory ResellerPayIntent.fromJson(Map<String, dynamic> json) =>
      ResellerPayIntent(
        providerType: json['provider_type'] as String? ?? 'paystack',
        reference: json['reference'].toString(),
        amount: asDouble(json['requested_amount']),
        currency: json['currency'] as String? ?? 'NGN',
        publicKey: json['provider_public_key'] as String?,
        metadata: ((json['checkout_metadata'] as Map?) ?? const {})
            .map((k, v) => MapEntry(k.toString(), v.toString())),
      );
}

/// Short-lived read-only customer token grant
/// (POST /reseller/accounts/{id}/impersonate).
class ResellerImpersonationGrant {
  ResellerImpersonationGrant({
    required this.accessToken,
    required this.accountId,
    required this.customerName,
    this.expiresAt,
  });

  final String accessToken;
  final String accountId;
  final String customerName;
  final DateTime? expiresAt;

  factory ResellerImpersonationGrant.fromJson(Map<String, dynamic> json) =>
      ResellerImpersonationGrant(
        accessToken: json['access_token'].toString(),
        accountId: json['account_id'].toString(),
        customerName: json['customer_name'] as String? ?? 'Customer',
        expiresAt: json['expires_at'] == null
            ? null
            : DateTime.tryParse(json['expires_at'].toString())?.toLocal(),
      );
}
