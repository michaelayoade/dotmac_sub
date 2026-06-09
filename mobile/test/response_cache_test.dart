import 'dart:io';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/core/response_cache.dart';
import 'package:flutter_test/flutter_test.dart';

/// Returns canned responses (or throws) per request so the interceptor's
/// transport behaviour can be exercised without a network.
class _FakeAdapter implements HttpClientAdapter {
  _FakeAdapter(this.onFetch);

  Future<ResponseBody> Function(RequestOptions options) onFetch;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<List<int>>? requestStream,
    Future<void>? cancelFuture,
  ) =>
      onFetch(options);

  @override
  void close({bool force = false}) {}
}

ResponseBody _json(String body, int status) => ResponseBody.fromString(
      body,
      status,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );

void main() {
  late Directory tmp;
  late ResponseCache cache;

  setUp(() async {
    tmp = await Directory.systemTemp.createTemp('api_cache_test');
    cache = ResponseCache(directory: tmp);
  });

  tearDown(() async {
    if (await tmp.exists()) await tmp.delete(recursive: true);
  });

  group('ResponseCache', () {
    test('write then read round-trips JSON', () async {
      await cache.write('GET /me/x', {
        'items': [
          {'id': '1'}
        ],
        'count': 1,
      });
      final out = await cache.read('GET /me/x');
      expect(out, isA<Map>());
      expect((out as Map)['count'], 1);
    });

    test('read misses return null; clear wipes entries', () async {
      expect(await cache.read('GET /missing'), isNull);
      await cache.write('GET /me/x', {'a': 'b'});
      await cache.clear();
      expect(await cache.read('GET /me/x'), isNull);
    });
  });

  group('CacheInterceptor', () {
    Dio buildDio(_FakeAdapter adapter) {
      final dio = Dio(BaseOptions(
        baseUrl: 'http://test.local/api/v1',
        // Mirror ApiClient: 4xx come back as responses, only 5xx/transport throw.
        validateStatus: (s) => s != null && s < 500,
      ));
      dio.interceptors.add(CacheInterceptor(cache));
      dio.httpClientAdapter = adapter;
      return dio;
    }

    test('writes through on a successful GET, then serves it on a timeout',
        () async {
      var fail = false;
      final dio = buildDio(_FakeAdapter((o) async {
        if (fail) {
          throw DioException(
            requestOptions: o,
            type: DioExceptionType.receiveTimeout,
          );
        }
        return _json('{"count":1}', 200);
      }));

      final ok = await dio.get('/me/subscriptions',
          queryParameters: {'limit': 50, 'offset': 0});
      expect(ok.statusCode, 200);

      // The write is fire-and-forget; wait for it to land.
      for (var i = 0; i < 50; i++) {
        if (await cache.read('GET /me/subscriptions?limit=50&offset=0') !=
            null) {
          break;
        }
        await Future<void>.delayed(const Duration(milliseconds: 5));
      }

      fail = true;
      final stale = await dio.get('/me/subscriptions',
          queryParameters: {'limit': 50, 'offset': 0});
      expect(stale.statusCode, 200);
      expect(stale.data, {'count': 1});
      expect(stale.extra['fromCache'], isTrue);
    });

    test('serves stale on a 5xx', () async {
      await cache.write('GET /me/balance?', {'balance': '10'});
      final dio = buildDio(_FakeAdapter((o) async => _json('err', 500)));

      final res = await dio.get('/me/balance');
      expect(res.statusCode, 200);
      expect(res.data, {'balance': '10'});
      expect(res.extra['fromCache'], isTrue);
    });

    test('does NOT serve stale on a 4xx (real answer must surface)', () async {
      await cache.write('GET /me/thing?', {'cached': 'true'});
      final dio =
          buildDio(_FakeAdapter((o) async => _json('{"detail":"no"}', 404)));

      final res = await dio.get('/me/thing');
      expect(res.statusCode, 404);
      expect(res.extra['fromCache'], anyOf(isNull, isFalse));
    });

    test('errors when nothing is cached', () async {
      final dio = buildDio(_FakeAdapter((o) async => throw DioException(
            requestOptions: o,
            type: DioExceptionType.connectionError,
          )));

      await expectLater(
        dio.get('/me/uncached'),
        throwsA(isA<DioException>()),
      );
    });
  });
}
