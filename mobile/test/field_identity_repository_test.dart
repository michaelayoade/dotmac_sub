import 'dart:convert';
import 'dart:typed_data';
import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/core/api_exception.dart';
import 'package:dotmac_portal/src/core/response_cache.dart';
import 'package:dotmac_portal/src/models/field_identity.dart';
import 'package:dotmac_portal/src/repositories/field_identity_repository.dart';
import 'package:flutter_test/flutter_test.dart';

const job = '11111111-1111-1111-1111-111111111111';
const generation = '22222222-2222-2222-2222-222222222222';
Map<String, dynamic> approvedJson() => {
      'work_order_id': job,
      'unavailable_reason': null,
      'card': {
        'display_name': 'Synthetic engineer',
        'company_name': 'Synthetic company',
        'verification_state': 'reviewed',
        'reviewed_at': '2026-10-11T10:00:00Z',
        'portrait_revision': 3,
        'assignment_generation': generation
      }
    };

class _Adapter implements HttpClientAdapter {
  _Adapter(this.fetcher);
  final Future<ResponseBody> Function(RequestOptions) fetcher;
  @override
  Future<ResponseBody> fetch(RequestOptions options, Stream<Uint8List>? stream,
          Future<void>? cancel) =>
      fetcher(options);
  @override
  void close({bool force = false}) {}
}

class _Cache extends ResponseCache {
  int reads = 0, writes = 0;
  @override
  Future<void> write(String key, Object? data) async {
    writes++;
  }

  @override
  Future<Object?> read(String key) async {
    reads++;
    return approvedJson();
  }
}

Dio dioFor(Future<ResponseBody> Function(RequestOptions) fetch) {
  final dio = Dio(BaseOptions(
      baseUrl: 'https://example.invalid',
      validateStatus: (s) => s != null && s < 500));
  dio.httpClientAdapter = _Adapter(fetch);
  return dio;
}

ResponseBody jsonBody(Object body, int status) =>
    ResponseBody.fromString(jsonEncode(body), status, headers: {
      Headers.contentTypeHeader: [Headers.jsonContentType]
    });
void main() {
  test(
      'native card/portrait use authenticated Dio with requireFresh and pinned revision',
      () async {
    final seen = <RequestOptions>[];
    final dio = dioFor((o) async {
      seen.add(o);
      return o.path.endsWith('/portrait')
          ? ResponseBody.fromBytes([1, 2, 3], 200,
              headers: {
                Headers.contentTypeHeader: ['image/png']
              })
          : jsonBody(approvedJson(), 200);
    });
    dio.options.headers['Authorization'] = 'Bearer synthetic';
    final cache = _Cache();
    dio.interceptors.add(CacheInterceptor(cache));
    final repo = FieldIdentityRepository(dio);
    final identity = (await repo.card(job)).approved!;
    expect(await repo.portrait(job, identity), [1, 2, 3]);
    expect(seen[0].path, '/me/work-orders/$job/field-identity');
    expect(seen[1].path, '/me/work-orders/$job/field-identity/portrait');
    expect(seen[1].queryParameters,
        {'assignment_generation': generation, 'portrait_revision': 3});
    expect(seen.every((o) => o.extra['requireFresh'] == true), isTrue);
    expect(seen[1].responseType, ResponseType.bytes);
    expect(seen.every((o) => o.headers['Authorization'] == 'Bearer synthetic'),
        isTrue);
    expect(seen.every((o) => !o.uri.toString().contains('synthetic')), isTrue);
    await Future<void>.delayed(Duration.zero);
    expect(cache.writes, 0);
    expect(cache.reads, 0);
  });
  for (final status in [403, 404, 409, 500]) {
    test('server $status never returns cached approved card or portrait',
        () async {
      final dio = dioFor((o) async => jsonBody({'detail': 'refused'}, status));
      final cache = _Cache();
      dio.interceptors.add(CacheInterceptor(cache));
      final repo = FieldIdentityRepository(dio);
      final card =
          FieldIdentityResult.fromJson(approvedJson(), expectedWorkOrderId: job)
              .approved!;
      await expectLater(repo.card(job), throwsA(isA<ApiException>()));
      await expectLater(repo.portrait(job, card), throwsA(isA<ApiException>()));
      expect(cache.reads, 0);
      expect(cache.writes, 0);
    });
  }
  test('transport failure never falls back to an old approval or photo',
      () async {
    final dio = dioFor((o) async => throw DioException(
        requestOptions: o, type: DioExceptionType.receiveTimeout));
    final cache = _Cache();
    dio.interceptors.add(CacheInterceptor(cache));
    await expectLater(
        FieldIdentityRepository(dio).card(job), throwsA(isA<ApiException>()));
    expect(cache.reads, 0);
  });
  test('unknown/revoked states and wrong work order fail closed', () {
    for (final state in ['pending', 'revoked', 'rejected', 'unknown']) {
      final data = approvedJson();
      (data['card'] as Map)['verification_state'] = state;
      expect(
          FieldIdentityResult.fromJson(data, expectedWorkOrderId: job).approved,
          isNull);
    }
    expect(
        () => FieldIdentityResult.fromJson(approvedJson(),
            expectedWorkOrderId: generation),
        throwsFormatException);
  });
  test('public job code cannot be used as native identity path', () async {
    final repo = FieldIdentityRepository(
        dioFor((o) async => throw StateError('transport must not run')));
    await expectLater(repo.card('WO-123'), throwsFormatException);
  });
}
