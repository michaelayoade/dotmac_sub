import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/invoice.dart';
import '../models/ledger.dart';
import '../models/page.dart';
import '../models/payment_flow.dart';
import '../models/payment_method.dart';
import '../models/topup.dart';

/// Wraps the billing endpoints (app/api/billing.py, mounted at /api/v1).
class BillingRepository {
  BillingRepository(this.dio);

  final Dio dio;

  /// GET /me/invoices — the signed-in subscriber's own invoices (self-scoped).
  Future<Page<Invoice>> invoices({
    String? status,
    int limit = 50,
    int offset = 0,
  }) async {
    final data = await guard(() => dio.get('/me/invoices', queryParameters: {
          if (status != null) 'status': status,
          'limit': limit,
          'offset': offset,
        }));
    return Page.fromJson(data as Map<String, dynamic>, Invoice.fromJson);
  }

  /// GET /me/invoices/{id} — detail for one of the subscriber's own invoices.
  Future<Invoice> invoice(String id) async {
    final data = await guard(() => dio.get('/me/invoices/$id'));
    return Invoice.fromJson(data as Map<String, dynamic>);
  }

  /// GET /me/payments — the subscriber's own payment history (self-scoped).
  Future<Page<Payment>> payments({int limit = 50, int offset = 0}) async {
    final data = await guard(() => dio.get('/me/payments', queryParameters: {
          'limit': limit,
          'offset': offset,
        }));
    return Page.fromJson(data as Map<String, dynamic>, Payment.fromJson);
  }

  /// GET /me/payment-methods — the subscriber's saved cards.
  Future<List<SavedCard>> paymentMethods() async {
    final data = await guard(() => dio.get('/me/payment-methods'));
    return (data as List)
        .cast<Map<String, dynamic>>()
        .map(SavedCard.fromJson)
        .toList();
  }

  /// PATCH /me/payment-methods/{id}/default — make a card the default.
  Future<void> setDefaultCard(String id) async {
    await guard(() => dio.patch('/me/payment-methods/$id/default'));
  }

  /// GET /me/autopay — current autopay status.
  Future<AutopayStatus> autopayStatus() async {
    final data = await guard(() => dio.get('/me/autopay'));
    return AutopayStatus.fromJson((data as Map).cast<String, dynamic>());
  }

  /// POST /me/autopay — enable autopay (default card unless one is given).
  Future<AutopayStatus> enableAutopay({String? paymentMethodId}) async {
    final data = await guard(() => dio.post('/me/autopay', data: {
          if (paymentMethodId != null) 'payment_method_id': paymentMethodId,
        }));
    return AutopayStatus.fromJson((data as Map).cast<String, dynamic>());
  }

  /// DELETE /me/autopay — disable autopay.
  Future<AutopayStatus> disableAutopay() async {
    final data = await guard(() => dio.delete('/me/autopay'));
    return AutopayStatus.fromJson((data as Map).cast<String, dynamic>());
  }

  /// DELETE /me/payment-methods/{id} — remove a saved card.
  Future<void> removeCard(String id) async {
    await guard(() => dio.delete('/me/payment-methods/$id'));
  }

  /// GET /me/balance — the subscriber's wallet/credit balance.
  Future<AccountBalance> balance() async {
    final data = await guard(() => dio.get('/me/balance'));
    return AccountBalance.fromJson((data as Map).cast<String, dynamic>());
  }

  /// GET /me/ledger — the subscriber's account ledger (transaction history).
  Future<Page<LedgerTxn>> ledger({int limit = 50, int offset = 0}) async {
    final data = await guard(() => dio.get('/me/ledger', queryParameters: {
          'limit': limit,
          'offset': offset,
        }));
    return Page.fromJson(data as Map<String, dynamic>, LedgerTxn.fromJson);
  }

  /// GET /dashboard — free-form stats map for the current consumer.
  Future<Map<String, dynamic>> dashboard() async {
    final data = await guard(() => dio.get('/dashboard'));
    return (data as Map).cast<String, dynamic>();
  }

  /// POST /payments/initiate — begin hosted checkout for one of my invoices.
  Future<PaymentInitiation> initiatePayment(String invoiceId) async {
    final data = await guard(() => dio.post(
          '/payments/initiate',
          data: {'invoice_id': invoiceId},
        ));
    return PaymentInitiation.fromJson(data as Map<String, dynamic>);
  }

  /// POST /payments/verify — confirm + record a completed provider transaction.
  Future<PaymentVerification> verifyPayment(
    String reference, {
    String? provider,
  }) async {
    final data = await guard(() => dio.post(
          '/payments/verify',
          data: {
            'reference': reference,
            if (provider != null) 'provider': provider,
          },
        ));
    return PaymentVerification.fromJson(data as Map<String, dynamic>);
  }

  /// GET /me/topup — balance, amount limits, presets, provider.
  Future<TopupPage> topupPage() async {
    final data = await guard(() => dio.get('/me/topup'));
    return TopupPage.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/topup/initiate — start a prepaid top-up checkout.
  Future<TopupInitiation> initiateTopup(num amount) async {
    final data = await guard(
        () => dio.post('/me/topup/initiate', data: {'amount': amount}));
    return TopupInitiation.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/topup/verify — confirm + credit the account.
  Future<TopupResult> verifyTopup(String reference) async {
    final data = await guard(
        () => dio.post('/me/topup/verify', data: {'reference': reference}));
    return TopupResult.fromJson(data as Map<String, dynamic>);
  }
}
