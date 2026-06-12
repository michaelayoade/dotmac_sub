import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/core/api_client.dart';
import 'package:dotmac_portal/src/core/token_storage.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

/// Drives [ApiClient.dio] without any network by returning canned responses
/// (or throwing) per request. The interceptor under test sees real Dio
/// behaviour; only the transport is faked.
class _FakeAdapter implements HttpClientAdapter {
  _FakeAdapter(this.onFetch);

  final Future<ResponseBody> Function(RequestOptions options) onFetch;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) => onFetch(options);

  @override
  void close({bool force = false}) {}
}

ResponseBody _json(Map<String, dynamic> body, int status) =>
    ResponseBody.fromString(
      // dio's default transformer JSON-decodes when content-type says so.
      _encode(body),
      status,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );

String _encode(Map<String, dynamic> body) =>
    '{${body.entries.map((e) => '"${e.key}":"${e.value}"').join(',')}}';

void main() {
  const storageChannel = MethodChannel(
    'plugins.it_nomads.com/flutter_secure_storage',
  );

  late Map<String, String> store;

  setUp(() {
    TestWidgetsFlutterBinding.ensureInitialized();
    store = {'access_token': 'expired', 'refresh_token': 'r1'};
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(storageChannel, (call) async {
          final args = (call.arguments as Map?) ?? const {};
          final key = args['key'] as String?;
          switch (call.method) {
            case 'read':
              return store[key];
            case 'write':
              store[key!] = args['value'] as String;
              return null;
            case 'delete':
              store.remove(key);
              return null;
            case 'deleteAll':
              store.clear();
              return null;
            case 'containsKey':
              return store.containsKey(key);
            case 'readAll':
              return Map<String, String>.from(store);
          }
          return null;
        });
  });

  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(storageChannel, null);
  });

  test('replay failing after a successful refresh surfaces the real error, '
      'not the stale 401', () async {
    var sessionExpired = false;
    final api = ApiClient(
      storage: TokenStorage(),
      onSessionExpired: () => sessionExpired = true,
    );
    api.dio.httpClientAdapter = _FakeAdapter((options) async {
      if (options.path == '/auth/refresh') {
        return _json({'access_token': 'fresh', 'refresh_token': 'r2'}, 200);
      }
      // First hit on the protected path -> 401 (expired token).
      if (options.extra['authRetried'] != true) {
        return _json({'detail': 'Unauthorized'}, 401);
      }
      // The replay (post-refresh) times out under load.
      throw DioException(
        requestOptions: options,
        type: DioExceptionType.receiveTimeout,
        message: 'simulated overload timeout',
      );
    });

    // Before the fix this resolved with the stale 401 Response; now the
    // replay's timeout must propagate so the UI shows a retryable state.
    await expectLater(
      api.dio.get('/me/subscriptions'),
      throwsA(
        isA<DioException>().having(
          (e) => e.type,
          'type',
          DioExceptionType.receiveTimeout,
        ),
      ),
    );
    // The token was genuinely refreshed, so this is not a session expiry.
    expect(sessionExpired, isFalse);
  });

  test('successful refresh + replay returns the replayed response', () async {
    final api = ApiClient(storage: TokenStorage());
    api.dio.httpClientAdapter = _FakeAdapter((options) async {
      if (options.path == '/auth/refresh') {
        return _json({'access_token': 'fresh', 'refresh_token': 'r2'}, 200);
      }
      if (options.extra['authRetried'] != true) {
        return _json({'detail': 'Unauthorized'}, 401);
      }
      return _json({'ok': 'true'}, 200);
    });

    final res = await api.dio.get('/me/subscriptions');
    expect(res.statusCode, 200);
  });

  test('refresh failure delivers the 401 and signals session expiry', () async {
    store.remove('refresh_token'); // no refresh token -> unrecoverable
    var sessionExpired = false;
    final api = ApiClient(
      storage: TokenStorage(),
      onSessionExpired: () => sessionExpired = true,
    );
    api.dio.httpClientAdapter = _FakeAdapter((options) async {
      return _json({'detail': 'Unauthorized'}, 401);
    });

    final res = await api.dio.get('/me/subscriptions');
    expect(res.statusCode, 401);
    expect(sessionExpired, isTrue);
  });
}
