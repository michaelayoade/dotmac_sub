import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/repositories/quotes_repository.dart';
import 'package:dotmac_portal/src/models/service_request_option.dart';
import 'package:flutter_test/flutter_test.dart';

class _FakeAdapter implements HttpClientAdapter {
  final List<RequestOptions> calls = [];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    calls.add(options);
    if (options.method == 'GET') {
      return _json({
        'quotes': const [],
        'total': 0,
        'open': 0,
        'source_state': 'retired',
        'actions_available': false,
        'actions_unavailable_message': 'Please contact support.',
      });
    }
    if (options.path.endsWith('/deposit/initiate')) {
      return _json({
        'invoice_id': 'invoice-1',
        'quote_id': 'quote-1',
        'amount': '25000.00',
        'currency': 'NGN',
        'provider_type': 'paystack',
        'payment_reference': 'DMAC-QUOTE-1',
        'charged': false,
      });
    }
    if (options.path.endsWith('/relocation/prepare')) {
      return _json({
        'request_id': 'change-1',
        'invoice_id': 'relocation-invoice-1',
        'amount': '120000.00',
        'currency': 'NGN',
        'replayed': false,
      });
    }
    return _json({
      'id': 'quote-1',
      'status': 'draft',
      'deposit_amount': '0',
      'feasibility': const {},
    }, statusCode: 201);
  }

  ResponseBody _json(Object body, {int statusCode = 200}) =>
      ResponseBody.fromString(
        jsonEncode(body),
        statusCode,
        headers: {
          Headers.contentTypeHeader: [Headers.jsonContentType],
        },
      );

  @override
  void close({bool force = false}) {}
}

void main() {
  late _FakeAdapter adapter;
  late QuotesRepository repository;

  setUp(() {
    adapter = _FakeAdapter();
    final dio = Dio(BaseOptions(baseUrl: 'https://selfcare.example.test'));
    dio.httpClientAdapter = adapter;
    repository = QuotesRepository(dio);
  });

  test('retains quote availability metadata from the list response', () async {
    final page = await repository.quotes();

    expect(page.actionsAvailable, isFalse);
    expect(page.actionsUnavailableMessage, 'Please contact support.');
  });

  test('sends a manually confirmed address separately from notes', () async {
    await repository.requestQuote(
      serviceOption: ServiceRequestOption.fiberToFiberRelocation,
      subscriptionId: 'subscription-1',
      destinationOfferId: 'offer-2',
      latitude: 9.0765,
      longitude: 7.3986,
      address: '12 Mississippi Street, Maitama',
      note: 'Blue gate, second floor',
    );

    final body = adapter.calls.single.data as Map<String, dynamic>;
    expect(body['address'], '12 Mississippi Street, Maitama');
    expect(body['note'], 'Blue gate, second floor');
    expect(body['service_option'], 'fiber_to_fiber_relocation');
    expect(body['subscription_id'], 'subscription-1');
    expect(body['destination_offer_id'], 'offer-2');
  });

  test(
    'deposit initiation carries retry evidence without a client callback',
    () async {
      await repository.initiateDeposit('quote-1');

      final body = adapter.calls.single.data as Map<String, dynamic>;
      expect(body['idempotency_key'], startsWith('quote-quote-1-'));
      expect((body['idempotency_key'] as String).length, greaterThan(16));
      expect(body.containsKey('redirect_url'), isFalse);
    },
  );

  test('prepares the canonical full-charge relocation invoice', () async {
    final prepared = await repository.prepareRelocation('quote-1');

    expect(adapter.calls.single.path,
        endsWith('/me/quotes/quote-1/relocation/prepare'));
    expect(prepared.invoiceId, 'relocation-invoice-1');
    expect(prepared.amount, '120000.00');
  });
}
