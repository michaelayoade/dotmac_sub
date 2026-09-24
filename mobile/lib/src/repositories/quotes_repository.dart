import 'dart:math';

import 'package:dio/dio.dart';

import '../config/env.dart';
import '../core/http.dart';
import '../models/quote.dart';
import '../models/service_request_option.dart';

/// Wraps the self-scoped self-serve quote endpoints (app/api/me.py, /me/quotes*):
/// request a map-pinned installation quote, list quotes, and pay the deposit via
/// the existing billing/pay flow.
class QuotesRepository {
  QuotesRepository(this.dio);

  final Dio dio;

  /// GET /me/quotes
  Future<QuotesPage> quotes() async {
    final data = await guard(() => dio.get('/me/quotes'));
    return QuotesPage.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/quote-request — drop a pin; the active quote owner returns
  /// feasibility and an estimate.
  Future<Quote> requestQuote({
    required ServiceRequestOption serviceOption,
    String? subscriptionId,
    String? destinationOfferId,
    required double latitude,
    required double longitude,
    String? address,
    String? region,
    String? note,
  }) async {
    final data = await guard(
      () => dio.post(
        '/me/quote-request',
        data: {
          'service_option': serviceOption.apiValue,
          if (subscriptionId != null) 'subscription_id': subscriptionId,
          if (destinationOfferId != null)
            'destination_offer_id': destinationOfferId,
          'latitude': latitude,
          'longitude': longitude,
          if (address != null && address.isNotEmpty) 'address': address,
          if (region != null && region.isNotEmpty) 'region': region,
          if (note != null && note.isNotEmpty) 'note': note,
        },
      ),
    );
    return Quote.fromJson(data as Map<String, dynamic>);
  }

  /// Prepare the canonical full-charge invoice for an approved relocation.
  Future<RelocationQuotePreparation> prepareRelocation(String quoteId) async {
    final data = await guard(
      () => dio.post('/me/quotes/$quoteId/relocation/prepare'),
    );
    return RelocationQuotePreparation.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/quotes/{id}/deposit/initiate — start the deposit checkout.
  Future<QuoteDepositInitiation> initiateDeposit(
    String quoteId, {
    String? provider,
  }) async {
    final idempotencyKey =
        'quote-$quoteId-${DateTime.now().microsecondsSinceEpoch}-'
        '${Random.secure().nextInt(1 << 32)}';
    final data = await guard(
      () => dio.post(
        '/me/quotes/$quoteId/deposit/initiate',
        data: {
          if (provider != null) 'provider': provider,
          'redirect_url': '${Brand.paymentScheme}://success',
          'idempotency_key': idempotencyKey,
        },
      ),
    );
    return QuoteDepositInitiation.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/quotes/{id}/deposit/verify — confirm payment; on settlement the
  /// quote is accepted natively (sales order + install project).
  Future<QuoteDepositResult> verifyDeposit(
    String quoteId, {
    required String reference,
    String? provider,
  }) async {
    final data = await guard(
      () => dio.post(
        '/me/quotes/$quoteId/deposit/verify',
        data: {
          'reference': reference,
          if (provider != null) 'provider': provider,
        },
      ),
    );
    return QuoteDepositResult.fromJson(data as Map<String, dynamic>);
  }
}
