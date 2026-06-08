/// Mirrors InvoiceRead from app/schemas/billing.py.
class Invoice {
  Invoice({
    required this.id,
    required this.accountId,
    required this.status,
    required this.currency,
    required this.subtotal,
    required this.taxTotal,
    required this.total,
    required this.balanceDue,
    this.invoiceNumber,
    this.issuedAt,
    this.dueAt,
    this.paidAt,
    this.memo,
  });

  final String id;
  final String accountId;
  final String status;
  final String currency;
  final double subtotal;
  final double taxTotal;
  final double total;
  final double balanceDue;
  final String? invoiceNumber;
  final DateTime? issuedAt;
  final DateTime? dueAt;
  final DateTime? paidAt;
  final String? memo;

  bool get isPaid => status == 'paid' || balanceDue <= 0;
  bool get isOverdue =>
      !isPaid && dueAt != null && dueAt!.isBefore(DateTime.now());

  factory Invoice.fromJson(Map<String, dynamic> json) => Invoice(
        id: json['id'].toString(),
        accountId: json['account_id'].toString(),
        status: json['status'] as String? ?? 'draft',
        currency: json['currency'] as String? ?? 'NGN',
        subtotal: _toDouble(json['subtotal']),
        taxTotal: _toDouble(json['tax_total']),
        total: _toDouble(json['total']),
        balanceDue: _toDouble(json['balance_due']),
        invoiceNumber: json['invoice_number'] as String?,
        issuedAt: _toDate(json['issued_at']),
        dueAt: _toDate(json['due_at']),
        paidAt: _toDate(json['paid_at']),
        memo: json['memo'] as String?,
      );
}

/// Mirrors PaymentRead from app/schemas/billing.py.
class Payment {
  Payment({
    required this.id,
    required this.amount,
    required this.currency,
    required this.status,
    this.paidAt,
    this.memo,
    this.externalId,
  });

  final String id;
  final double amount;
  final String currency;
  final String status;
  final DateTime? paidAt;
  final String? memo;
  final String? externalId;

  factory Payment.fromJson(Map<String, dynamic> json) => Payment(
        id: json['id'].toString(),
        amount: _toDouble(json['amount']),
        currency: json['currency'] as String? ?? 'NGN',
        status: json['status'] as String? ?? 'pending',
        paidAt: _toDate(json['paid_at']),
        memo: json['memo'] as String?,
        externalId: json['external_id'] as String?,
      );
}

double _toDouble(dynamic v) {
  if (v == null) return 0;
  if (v is num) return v.toDouble();
  return double.tryParse(v.toString()) ?? 0; // Decimal serialises as string
}

DateTime? _toDate(dynamic v) {
  if (v == null) return null;
  return DateTime.tryParse(v.toString())?.toLocal();
}
