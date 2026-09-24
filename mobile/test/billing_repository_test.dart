import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/core/api_exception.dart';
import 'package:dotmac_portal/src/repositories/billing_repository.dart';
import 'package:flutter_test/flutter_test.dart';

class _FakeAdapter implements HttpClientAdapter {
  _FakeAdapter(this.onFetch);

  final ResponseBody Function(RequestOptions options) onFetch;
  final List<RequestOptions> calls = [];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    calls.add(options);
    await requestStream?.drain<void>();
    return onFetch(options);
  }

  @override
  void close({bool force = false}) {}
}

Dio _dio(_FakeAdapter adapter) {
  final dio = Dio(BaseOptions(baseUrl: 'https://test.local/api/v1'));
  dio.httpClientAdapter = adapter;
  return dio;
}

ResponseBody _pdf({String filename = 'invoice-INV-42.pdf'}) =>
    ResponseBody.fromBytes(
      Uint8List.fromList('%PDF-test'.codeUnits),
      200,
      headers: {
        Headers.contentTypeHeader: ['application/pdf'],
        'content-disposition': ['attachment; filename="$filename"'],
      },
    );

void main() {
  test('payment proof submission carries intent and collection account IDs',
      () async {
    final receipt = File(
      '${Directory.systemTemp.path}/dotmac-payment-proof-${DateTime.now().microsecondsSinceEpoch}.jpg',
    );
    await receipt.writeAsBytes([1, 2, 3]);
    late FormData submitted;
    final adapter = _FakeAdapter((options) {
      submitted = options.data as FormData;
      return ResponseBody.fromString(
        jsonEncode({
          'id': 'proof-1',
          'amount': '5000.00',
          'currency': 'NGN',
          'status': 'submitted',
        }),
        200,
        headers: {
          Headers.contentTypeHeader: ['application/json'],
        },
      );
    });

    try {
      await BillingRepository(_dio(adapter)).submitPaymentProof(
        intentId: 'intent-1',
        selectedAccountId: 'collection-account-1',
        filePath: receipt.path,
        fileName: 'receipt.jpg',
      );
    } finally {
      await receipt.delete();
    }

    expect(Map.fromEntries(submitted.fields)['intent_id'], 'intent-1');
    expect(
      Map.fromEntries(submitted.fields)['selected_account_id'],
      'collection-account-1',
    );
  });

  test('cancel direct transfer uses the self-scoped intent endpoint', () async {
    final adapter = _FakeAdapter((_) => ResponseBody.fromString('', 204));

    await BillingRepository(_dio(adapter)).cancelTopupIntent('intent-2');

    expect(adapter.calls.single.method, 'POST');
    expect(adapter.calls.single.path, '/me/topup/intents/intent-2/cancel');
  });

  test('invoice PDF uses authenticated API path and server filename', () async {
    final adapter = _FakeAdapter((_) => _pdf());

    final document = await BillingRepository(_dio(adapter)).invoicePdf('i-1');

    expect(adapter.calls.single.path, '/me/invoices/i-1/pdf');
    expect(adapter.calls.single.responseType, ResponseType.bytes);
    expect(document.filename, 'invoice-INV-42.pdf');
    expect(document.bytes.sublist(0, 5), '%PDF-'.codeUnits);
  });

  test('payment receipt PDF uses the self-scoped receipt path', () async {
    final adapter = _FakeAdapter((_) => _pdf(filename: 'receipt-RCP-2.pdf'));

    final document =
        await BillingRepository(_dio(adapter)).paymentReceiptPdf('p-2');

    expect(adapter.calls.single.path, '/me/payments/p-2/receipt/pdf');
    expect(document.filename, 'receipt-RCP-2.pdf');
  });

  test('PDF download rejects a non-PDF response', () async {
    final adapter = _FakeAdapter(
      (_) => ResponseBody.fromString(
        '<html>login</html>',
        200,
        headers: {
          Headers.contentTypeHeader: ['text/html'],
        },
      ),
    );

    expect(
      BillingRepository(_dio(adapter)).invoicePdf('i-1'),
      throwsA(isA<ApiException>()),
    );
  });

  test('payment proof upload filename preserves supported picker name', () {
    expect(
      paymentProofUploadFilename(
        filePath: '/data/user/0/cache/scaled_image.jpg',
        fileName: 'receipt.PNG',
      ),
      'receipt.PNG',
    );
  });

  test('payment proof upload filename borrows supported path extension', () {
    expect(
      paymentProofUploadFilename(
        filePath: '/data/user/0/cache/scaled_image.png',
        fileName: 'image_picker_12345',
      ),
      'image_picker_12345.png',
    );
  });

  test('payment proof upload filename defaults extensionless images to jpg',
      () {
    expect(
      paymentProofUploadFilename(
        filePath: '/data/user/0/cache/image_picker_12345',
        fileName: '',
      ),
      'receipt.jpg',
    );
  });
}
