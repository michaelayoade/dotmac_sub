import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/repositories/push_repository.dart';
import 'package:flutter_test/flutter_test.dart';

/// Fakes the Dio transport so we assert the exact request the repository makes.
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

void main() {
  test('registerToken POSTs token + platform to /me/push-tokens', () async {
    final adapter = _FakeAdapter(
      (_) => ResponseBody.fromString(
        '{}',
        201,
        headers: {
          Headers.contentTypeHeader: [Headers.jsonContentType],
        },
      ),
    );
    final repo = PushRepository(_dio(adapter));

    await repo.registerToken(token: 'fcm-tok-123', platform: 'android');

    expect(adapter.calls, hasLength(1));
    final req = adapter.calls.single;
    expect(req.method, 'POST');
    expect(req.path, '/me/push-tokens');
    expect(req.data, {'token': 'fcm-tok-123', 'platform': 'android'});
  });

  test('unregisterToken DELETEs /me/push-tokens/{token}', () async {
    final adapter = _FakeAdapter((_) => ResponseBody.fromString('', 204));
    final repo = PushRepository(_dio(adapter));

    await repo.unregisterToken('fcm-tok-123');

    expect(adapter.calls, hasLength(1));
    final req = adapter.calls.single;
    expect(req.method, 'DELETE');
    expect(req.path, '/me/push-tokens/fcm-tok-123');
  });
}
