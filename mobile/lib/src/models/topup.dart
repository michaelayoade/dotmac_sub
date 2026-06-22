// Top-up (prepaid account credit) models mirroring app/api/me.py topup endpoints.

import '../core/parsers.dart';

/// An online checkout option (Paystack/Flutterwave) for the pay selector.
class PaymentProviderOption {
  PaymentProviderOption({required this.providerType, required this.label});

  final String providerType;
  final String label;

  factory PaymentProviderOption.fromJson(Map<String, dynamic> json) =>
      PaymentProviderOption(
        providerType: json['provider_type'] as String? ?? 'paystack',
        label: json['label'] as String? ?? 'Pay online',
      );
}

/// One admin-configured bank account shown under the bank-transfer option.
class BankAccount {
  BankAccount({
    required this.bankName,
    required this.accountName,
    required this.accountNumber,
  });

  final String bankName;
  final String accountName;
  final String accountNumber;

  factory BankAccount.fromJson(Map<String, dynamic> json) => BankAccount(
        bankName: json['bank_name'] as String? ?? '',
        accountName: json['account_name'] as String? ?? '',
        accountNumber: json['account_number'] as String? ?? '',
      );
}

/// Direct-bank-transfer config: the account(s) to pay into + instructions.
class BankTransferConfig {
  BankTransferConfig({
    this.enabled = false,
    this.instructions,
    this.accounts = const [],
  });

  final bool enabled;
  final String? instructions;
  final List<BankAccount> accounts;

  bool get hasAccounts => enabled && accounts.isNotEmpty;

  factory BankTransferConfig.fromJson(Map<String, dynamic>? json) {
    if (json == null) return BankTransferConfig();
    return BankTransferConfig(
      enabled: json['enabled'] as bool? ?? false,
      instructions: json['instructions'] as String?,
      accounts: (json['accounts'] as List? ?? const [])
          .cast<Map<String, dynamic>>()
          .map(BankAccount.fromJson)
          .toList(),
    );
  }
}

class TopupPage {
  TopupPage({
    required this.providerType,
    required this.currency,
    required this.minAmount,
    required this.maxAmount,
    this.providerPublicKey,
    this.prepaidBalance,
    this.presetAmounts = const [],
    this.customerEmail,
    this.providers = const [],
    BankTransferConfig? bankTransfer,
  }) : bankTransfer = bankTransfer ?? BankTransferConfig();

  final String providerType;
  final String currency;
  final int minAmount;
  final int maxAmount;
  final String? providerPublicKey;
  final double? prepaidBalance;
  final List<int> presetAmounts;
  final String? customerEmail;

  /// Online gateway options (Paystack/Flutterwave), default provider first.
  final List<PaymentProviderOption> providers;

  /// Direct bank-transfer option (admin bank account + receipt upload).
  final BankTransferConfig bankTransfer;

  factory TopupPage.fromJson(Map<String, dynamic> json) => TopupPage(
        providerType: json['provider_type'] as String? ?? 'paystack',
        currency: json['currency'] as String? ?? 'NGN',
        minAmount: (json['min_amount'] as num?)?.toInt() ?? 1000,
        maxAmount: (json['max_amount'] as num?)?.toInt() ?? 500000,
        providerPublicKey: json['provider_public_key'] as String?,
        prepaidBalance: asDoubleOrNull(json['prepaid_balance']),
        presetAmounts: (json['preset_amounts'] as List? ?? const [])
            .map((e) => (e as num).toInt())
            .toList(),
        customerEmail: json['customer_email'] as String?,
        providers: (json['payment_options'] as List? ?? const [])
            .cast<Map<String, dynamic>>()
            .map(PaymentProviderOption.fromJson)
            .toList(),
        bankTransfer: BankTransferConfig.fromJson(
            json['direct_bank_transfer'] as Map<String, dynamic>?),
      );
}

class TopupInitiation {
  TopupInitiation({
    required this.intentId,
    required this.providerType,
    required this.paymentReference,
    required this.amount,
    required this.currency,
    this.providerPublicKey,
    this.customerEmail,
    this.charged = false,
    this.checkoutUrl,
  });

  final String intentId;
  final String providerType;
  final String paymentReference;
  final double amount;
  final String currency;
  final String? providerPublicKey;
  final String? customerEmail;

  /// True when a saved card was charged server-side — skip the gateway webview
  /// and go straight to verify.
  final bool charged;
  final String? checkoutUrl;

  factory TopupInitiation.fromJson(Map<String, dynamic> json) =>
      TopupInitiation(
        intentId: json['intent_id'].toString(),
        providerType: json['provider_type'] as String? ?? 'paystack',
        paymentReference: json['payment_reference'].toString(),
        amount: asDouble(json['amount']),
        currency: json['currency'] as String? ?? 'NGN',
        providerPublicKey: json['provider_public_key'] as String?,
        customerEmail: json['customer_email'] as String?,
        charged: json['charged'] as bool? ?? false,
        checkoutUrl: json['checkout_url'] as String?,
      );
}

class TopupResult {
  TopupResult({
    required this.reference,
    required this.amount,
    this.alreadyRecorded = false,
    this.availableBalance,
    this.creditAdded,
  });

  final String reference;
  final double amount;
  final bool alreadyRecorded;
  final double? availableBalance;
  final double? creditAdded;

  factory TopupResult.fromJson(Map<String, dynamic> json) => TopupResult(
        reference: json['reference'].toString(),
        amount: asDouble(json['amount']),
        alreadyRecorded: json['already_recorded'] as bool? ?? false,
        availableBalance: asDoubleOrNull(json['available_balance']),
        creditAdded: asDoubleOrNull(json['credit_added']),
      );
}
