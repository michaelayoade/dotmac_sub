// Bank-transfer payment proofs (app/api/payment_proofs.py).

class PaymentProofItem {
  PaymentProofItem({
    required this.id,
    required this.amount,
    required this.currency,
    required this.status,
    this.bankName,
    this.reference,
    this.reviewNotes,
    this.createdAt,
  });

  final String id;
  final double amount;
  final String currency;

  /// submitted | verified | rejected
  final String status;
  final String? bankName;
  final String? reference;
  final String? reviewNotes;
  final DateTime? createdAt;

  factory PaymentProofItem.fromJson(Map<String, dynamic> json) =>
      PaymentProofItem(
        id: json['id'].toString(),
        amount: double.tryParse(json['amount'].toString()) ?? 0,
        currency: json['currency'] as String? ?? 'NGN',
        status: json['status'] as String? ?? 'submitted',
        bankName: json['bank_name'] as String?,
        reference: json['reference'] as String?,
        reviewNotes: json['review_notes'] as String?,
        createdAt: json['created_at'] == null
            ? null
            : DateTime.tryParse(json['created_at'].toString())?.toLocal(),
      );
}
