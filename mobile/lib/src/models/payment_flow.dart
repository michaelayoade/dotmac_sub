// Response from POST /payments/initiate.
import '../core/parsers.dart';

class PaymentInitiation {
  PaymentInitiation({
    required this.invoiceId,
    required this.amount,
    required this.currency,
    required this.providerType,
    required this.paymentReference,
    this.invoiceNumber,
    this.providerPublicKey,
    this.customerEmail,
  });

  final String invoiceId;
  final double amount;
  final String currency;
  final String providerType; // 'paystack' | 'flutterwave'
  final String paymentReference;
  final String? invoiceNumber;
  final String? providerPublicKey;
  final String? customerEmail;

  factory PaymentInitiation.fromJson(Map<String, dynamic> json) =>
      PaymentInitiation(
        invoiceId: json['invoice_id'].toString(),
        amount: asDouble(json['amount']),
        currency: json['currency'] as String? ?? 'NGN',
        providerType: json['provider_type'] as String? ?? 'paystack',
        paymentReference: json['payment_reference'].toString(),
        invoiceNumber: json['invoice_number'] as String?,
        providerPublicKey: json['provider_public_key'] as String?,
        customerEmail: json['customer_email'] as String?,
      );
}

/// Response from POST /payments/verify.
class PaymentVerification {
  PaymentVerification({
    required this.reference,
    required this.paymentId,
    required this.amount,
    required this.currency,
    required this.status,
    this.invoiceId,
    this.alreadyRecorded = false,
  });

  final String reference;
  final String paymentId;
  final double amount;
  final String currency;
  final String status;
  final String? invoiceId;
  final bool alreadyRecorded;

  bool get succeeded => status == 'succeeded';

  factory PaymentVerification.fromJson(Map<String, dynamic> json) =>
      PaymentVerification(
        reference: json['reference'].toString(),
        paymentId: json['payment_id'].toString(),
        amount: asDouble(json['amount']),
        currency: json['currency'] as String? ?? 'NGN',
        status: json['status'] as String? ?? 'succeeded',
        invoiceId: json['invoice_id']?.toString(),
        alreadyRecorded: json['already_recorded'] as bool? ?? false,
      );
}
