import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/models/field_visit_chat.dart';
import 'package:dotmac_portal/src/repositories/work_order_repository.dart';
import 'package:flutter_test/flutter_test.dart';

class _Adapter implements HttpClientAdapter {
  _Adapter(this.body, {this.status = 200});
  final String body;
  final int status;
  final List<RequestOptions> requests = [];

  @override
  Future<ResponseBody> fetch(RequestOptions options,
      Stream<Uint8List>? requestStream, Future<void>? cancelFuture) async {
    requests.add(options);
    return ResponseBody.fromString(body, status, headers: {
      Headers.contentTypeHeader: [Headers.jsonContentType]
    });
  }

  @override
  void close({bool force = false}) {}
}

WorkOrderRepository _repository(_Adapter adapter) {
  final dio = Dio(BaseOptions(baseUrl: 'https://selfcare.example.test/api/v1'));
  dio.httpClientAdapter = adapter;
  return WorkOrderRepository(dio);
}

void main() {
  test('missing eligibility fails closed', () {
    final chat = FieldVisitChat.fromJson({});
    expect(chat.available, isFalse);
    expect(chat.canSend, isFalse);
    expect(chat.messages, isEmpty);
  });

  test('closed visit keeps readable conversation without sending', () async {
    final adapter = _Adapter('{"available":true,"can_send":false,'
        '"messages":[{"id":"message-1","body":"Gate is open",'
        '"from_customer":true,"created_at":"2026-10-11T09:00:00Z"}]}');
    final chat = await _repository(adapter).chat('sub-visit-1');
    expect(adapter.requests.single.path, '/me/work-orders/sub-visit-1/chat');
    expect(chat.canSend, isFalse);
    expect(chat.messages.single.body, 'Gate is open');
    expect(chat.messages.single.fromCustomer, isTrue);
  });

  test('send uses scoped visit endpoint and performs one request', () async {
    final adapter = _Adapter(
        '{"id":"message-2","body":"At the gate",'
        '"from_customer":true,"created_at":"2026-10-11T09:01:00Z"}',
        status: 201);
    final message = await _repository(adapter).sendChatMessage('sub-visit-1',
        body: 'At the gate', requestId: 'request-1');
    expect(message.id, 'message-2');
    expect(adapter.requests, hasLength(1));
    expect(adapter.requests.single.method, 'POST');
    expect(adapter.requests.single.path,
        '/me/work-orders/sub-visit-1/chat/messages');
    expect(adapter.requests.single.data,
        {'body': 'At the gate', 'request_id': 'request-1'});
  });

  test('rejected send is not automatically replayed', () async {
    final adapter = _Adapter('{"detail":"Visit is closed"}', status: 409);
    await expectLater(
        _repository(adapter).sendChatMessage('sub-visit-1', body: 'Hello'),
        throwsA(isA<Exception>()));
    expect(adapter.requests, hasLength(1));
  });
}
