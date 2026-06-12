/// Mirrors VasWallet* schemas from app/schemas/vas.py.
class WalletEntry {
  WalletEntry({
    required this.id,
    required this.entryType,
    required this.category,
    required this.amount,
    required this.currency,
    this.memo,
    this.createdAt,
  });

  final String id;
  final String entryType; // 'credit' | 'debit'
  final String category;
  final double amount;
  final String currency;
  final String? memo;
  final DateTime? createdAt;

  bool get isCredit => entryType == 'credit';

  factory WalletEntry.fromJson(Map<String, dynamic> json) => WalletEntry(
        id: json['id'].toString(),
        entryType: json['entry_type'] as String? ?? 'credit',
        category: json['category'] as String? ?? 'adjustment',
        amount: double.tryParse(json['amount'].toString()) ?? 0,
        currency: json['currency'] as String? ?? 'NGN',
        memo: json['memo'] as String?,
        createdAt: json['created_at'] != null
            ? DateTime.tryParse(json['created_at'] as String)
            : null,
      );
}

class WalletOverview {
  WalletOverview({
    required this.balance,
    required this.currency,
    required this.autoPayBillEnabled,
    required this.minTopup,
    required this.maxTopup,
    required this.authThreshold,
    this.entries = const [],
  });

  final double balance;
  final String currency;
  final bool autoPayBillEnabled;
  final int minTopup;
  final int maxTopup;
  final int authThreshold;
  final List<WalletEntry> entries;

  factory WalletOverview.fromJson(Map<String, dynamic> json) => WalletOverview(
        balance: double.tryParse(json['balance'].toString()) ?? 0,
        currency: json['currency'] as String? ?? 'NGN',
        autoPayBillEnabled: json['auto_pay_bill_enabled'] as bool? ?? false,
        minTopup: (json['min_topup'] as num?)?.toInt() ?? 100,
        maxTopup: (json['max_topup'] as num?)?.toInt() ?? 50000,
        authThreshold: (json['auth_threshold'] as num?)?.toInt() ?? 5000,
        entries: [
          for (final item in (json['entries'] as List? ?? const []))
            WalletEntry.fromJson(item as Map<String, dynamic>),
        ],
      );
}

class WalletTopupInitiation {
  WalletTopupInitiation({
    required this.providerType,
    required this.reference,
    required this.amount,
    required this.currency,
    this.publicKey,
    this.customerEmail,
  });

  final String providerType;
  final String reference;
  final double amount;
  final String currency;
  final String? publicKey;
  final String? customerEmail;

  factory WalletTopupInitiation.fromJson(Map<String, dynamic> json) =>
      WalletTopupInitiation(
        providerType: json['provider_type'] as String? ?? 'paystack',
        reference: json['reference'] as String,
        amount: double.tryParse(json['amount'].toString()) ?? 0,
        currency: json['currency'] as String? ?? 'NGN',
        publicKey: json['provider_public_key'] as String?,
        customerEmail: json['customer_email'] as String?,
      );
}
