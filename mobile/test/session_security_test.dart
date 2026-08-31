import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/core/api_client.dart';
import 'package:dotmac_portal/src/core/api_exception.dart';
import 'package:dotmac_portal/src/core/biometric_service.dart';
import 'package:dotmac_portal/src/core/cache_crypto.dart';
import 'package:dotmac_portal/src/core/credential_bundle.dart';
import 'package:dotmac_portal/src/core/data_scope.dart';
import 'package:dotmac_portal/src/core/observability.dart';
import 'package:dotmac_portal/src/core/response_cache.dart';
import 'package:dotmac_portal/src/core/session_fence.dart';
import 'package:dotmac_portal/src/core/session_wipe.dart';
import 'package:dotmac_portal/src/core/token_storage.dart';
import 'package:dotmac_portal/src/models/auth.dart';
import 'package:dotmac_portal/src/providers/auth_controller.dart';
import 'package:dotmac_portal/src/repositories/auth_repository.dart';

/// Drives [ApiClient.dio] without any network by returning canned responses
/// (or throwing) per request. The interceptors under test see real Dio
/// behaviour; only the transport is faked.
class _FakeAdapter implements HttpClientAdapter {
  _FakeAdapter(this.onFetch);

  Future<ResponseBody> Function(RequestOptions options) onFetch;

  @override
  Future<ResponseBody> fetch(RequestOptions options,
          Stream<Uint8List>? requestStream, Future<void>? cancelFuture) =>
      onFetch(options);

  @override
  void close({bool force = false}) {}
}

ResponseBody _json(Map<String, dynamic> body, int status) =>
    ResponseBody.fromString(jsonEncode(body), status, headers: {
      Headers.contentTypeHeader: [Headers.jsonContentType]
    });

/// Biometric stub — the real one talks to a platform channel that does not
/// exist under `flutter test`.
class _NoBiometrics extends BiometricService {
  @override
  Future<bool> isAvailable() async => false;

  @override
  Future<bool> authenticate({required String reason}) async => false;
}

/// Auth repository stub — never hits the network.
class _FakeAuthRepository extends AuthRepository {
  _FakeAuthRepository(TokenStorage storage, {this.me0})
      : super(dio: Dio(), storage: storage);

  final Me? me0;
  Object? meThrows;
  int logoutCalls = 0;

  @override
  Future<Me> me() async {
    final err = meThrows;
    if (err != null) throw err;
    return me0 ?? Me(id: '1', firstName: 'A', lastName: 'B', email: 'a@b.c');
  }

  @override
  Future<void> logout() async => logoutCalls++;
}

void main() {
  const storageChannel =
      MethodChannel('plugins.it_nomads.com/flutter_secure_storage');

  late Map<String, String> store;

  /// Installs an in-memory stand-in for flutter_secure_storage, so the real
  /// TokenStorage and the real CacheCipher both run.
  void mockSecureStorage() {
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
  }

  setUp(() {
    TestWidgetsFlutterBinding.ensureInitialized();
    store = {};
    mockSecureStorage();
  });

  tearDown(() {
    Log.sink = null;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(storageChannel, null);
  });

  // ---------------------------------------------------------------------
  group('credential bundle', () {
    test('one session is one atomic secure-store record', () async {
      final ts = TokenStorage();
      await ts.beginSession(accessToken: 'a1', refreshToken: 'r1');

      // The whole session must be recoverable from ONE key. Two keys is the
      // shape that could tear mid-write and leave a mismatched pair.
      final sessionKeys = store.keys
          .where((k) => store[k] == 'a1' || store[k] == 'r1')
          .toList();
      expect(sessionKeys, isEmpty,
          reason: 'tokens must not be stored as bare per-token values');
      final holders = store.entries
          .where((e) => e.value.contains('a1') && e.value.contains('r1'))
          .toList();
      expect(holders, hasLength(1),
          reason: 'exactly one record carries the whole session');

      final bundle = await ts.readBundle();
      expect(bundle!.accessToken, 'a1');
      expect(bundle.refreshToken, 'r1');
      expect(bundle.generation, greaterThan(0));
    });

    test('generations never repeat, even across a wipe', () async {
      final ts = TokenStorage();
      final first = await ts.beginSession(accessToken: 'a1');
      await ts.clear();
      final second = await ts.beginSession(accessToken: 'a2');

      expect(second, greaterThan(first),
          reason: 'a reused generation would let a previous session\'s '
              'in-flight refresh pass the new session\'s fence');
    });

    test('a record from an unknown schema version is discarded whole',
        () async {
      // A downgrade: this build meets a record written by a newer one.
      store['session_bundle_v2'] = jsonEncode({
        'v': CredentialBundle.currentVersion + 1,
        'access_token': 'a-future',
        'refresh_token': 'r-future',
        'generation': 9
      });
      store['cached_profile'] = '{"id":"1"}';

      final ts = TokenStorage();
      expect(await ts.readBundle(), isNull,
          reason: 'never partially apply a record we do not understand');
      expect(store.containsKey('session_bundle_v2'), isFalse);
      expect(store.containsKey('cached_profile'), isFalse,
          reason: 'discarding credentials discards the profile with them');
    });

    test('a corrupt record is discarded rather than half-read', () async {
      store['session_bundle_v2'] = '{"v":2,"access_token":';
      final ts = TokenStorage();
      expect(await ts.readBundle(), isNull);
      expect(await ts.readAccessToken(), isNull);
    });

    test('a legacy two-key install migrates without signing the user out',
        () async {
      // Exactly what is on disk for an install of the previous app version.
      store['access_token'] = 'legacy-access';
      store['refresh_token'] = 'legacy-refresh';

      final ts = TokenStorage();
      final bundle = await ts.readBundle();

      expect(bundle, isNotNull, reason: 'the user stays signed in');
      expect(bundle!.accessToken, 'legacy-access');
      expect(bundle.refreshToken, 'legacy-refresh');
      expect(bundle.scope, MobileDataScope.anonymous,
          reason: 'the legacy keys never recorded whose tokens they were');
      // And the old keys are gone, so there is only ever one copy.
      expect(store.containsKey('access_token'), isFalse);
      expect(store.containsKey('refresh_token'), isFalse);

      // Re-reading is stable (the migration is not re-run).
      expect((await ts.readBundle())!.generation, bundle.generation);
    });

    test('identity is stamped on later, under the same generation', () async {
      final ts = TokenStorage();
      final generation = await ts.beginSession(accessToken: 'a1');
      const scope = MobileDataScope(tenant: 'https://x', principal: 'acct-1');

      expect(await ts.adoptScope(generation: generation, scope: scope), isTrue);
      expect((await ts.readBundle())!.scope, scope);
      expect((await ts.readBundle())!.generation, generation);

      // A stale caller cannot re-attribute the session.
      expect(
          await ts.adoptScope(
              generation: generation - 1,
              scope: const MobileDataScope(
                  tenant: 'https://x', principal: 'evil')),
          isFalse);
      expect((await ts.readBundle())!.scope, scope);
    });
  });

  // ---------------------------------------------------------------------
  group('token refresh', () {
    test('concurrent 401s cause exactly one refresh call', () async {
      store['access_token'] = 'expired';
      store['refresh_token'] = 'r1';

      var refreshCalls = 0;
      final refreshGate = Completer<void>();
      final api = ApiClient(storage: TokenStorage());
      api.dio.httpClientAdapter = _FakeAdapter((options) async {
        if (options.path == '/auth/refresh') {
          refreshCalls++;
          // Hold the refresh open so every 401 is guaranteed to arrive while
          // it is still in flight — the exact window a storm happens in.
          await refreshGate.future;
          return _json({'access_token': 'fresh', 'refresh_token': 'r2'}, 200);
        }
        if (options.extra['authRetried'] != true) {
          return _json({'detail': 'Unauthorized'}, 401);
        }
        return _json({'ok': true}, 200);
      });

      final calls = [
        api.dio.get('/me/subscriptions'),
        api.dio.get('/me/invoices'),
        api.dio.get('/me/balance'),
        api.dio.get('/me/usage')
      ];
      // Let all four reach their 401 and queue behind the single refresh.
      await pumpEventQueue();
      refreshGate.complete();
      final results = await Future.wait(calls);

      expect(refreshCalls, 1,
          reason: 'four concurrent 401s must coalesce onto one /auth/refresh');
      for (final res in results) {
        expect(res.statusCode, 200,
            reason: 'every waiter resumes on the shared result');
      }
      expect((await TokenStorage().readBundle())!.accessToken, 'fresh');
    });

    test('a 500 leaves the session intact', () async {
      store['access_token'] = 'a1';
      store['refresh_token'] = 'r1';

      var expired = false;
      final api = ApiClient(
          storage: TokenStorage(), onSessionExpired: () => expired = true);
      api.dio.httpClientAdapter =
          _FakeAdapter((o) async => _json({'detail': 'boom'}, 500));

      await expectLater(
          api.dio.get('/me/subscriptions'), throwsA(isA<DioException>()));

      expect(expired, isFalse, reason: 'a server fault is not a sign-out');
      final bundle = await TokenStorage().readBundle();
      expect(bundle!.accessToken, 'a1');
      expect(bundle.refreshToken, 'r1');
    });

    test('a connection timeout leaves the session intact', () async {
      store['access_token'] = 'a1';
      store['refresh_token'] = 'r1';

      var expired = false;
      final api = ApiClient(
          storage: TokenStorage(), onSessionExpired: () => expired = true);
      api.dio.httpClientAdapter = _FakeAdapter((o) async {
        throw DioException(
            requestOptions: o, type: DioExceptionType.connectionTimeout);
      });

      await expectLater(
          api.dio.get('/me/subscriptions'), throwsA(isA<DioException>()));

      expect(expired, isFalse);
      expect((await TokenStorage().readBundle())!.accessToken, 'a1');
    });

    test('a 401 whose refresh cannot be delivered does NOT sign the user out',
        () async {
      store['access_token'] = 'expired';
      store['refresh_token'] = 'r1';

      var expired = false;
      final api = ApiClient(
          storage: TokenStorage(), onSessionExpired: () => expired = true);
      api.dio.httpClientAdapter = _FakeAdapter((o) async {
        if (o.path == '/auth/refresh') {
          // The network died between the 401 and the refresh.
          throw DioException(
              requestOptions: o, type: DioExceptionType.connectionError);
        }
        return _json({'detail': 'Unauthorized'}, 401);
      });

      final res = await api.dio.get('/me/subscriptions');
      expect(res.statusCode, 401, reason: 'the caller still sees the failure');
      expect(expired, isFalse,
          reason: 'could-not-ask is not the server saying no');
      expect((await TokenStorage().readBundle())!.accessToken, 'expired',
          reason: 'credentials survive so a retry can recover the session');
    });

    test('a refresh the server REFUSES does sign the user out', () async {
      store['access_token'] = 'expired';
      store['refresh_token'] = 'revoked';

      var expired = false;
      final api = ApiClient(
          storage: TokenStorage(), onSessionExpired: () => expired = true);
      api.dio.httpClientAdapter = _FakeAdapter((o) async {
        if (o.path == '/auth/refresh') {
          return _json({'detail': 'Refresh token revoked'}, 401);
        }
        return _json({'detail': 'Unauthorized'}, 401);
      });

      await api.dio.get('/me/subscriptions');
      expect(expired, isTrue);
    });

    test('a freshly-issued token rejected on first use is authoritative',
        () async {
      store['access_token'] = 'expired';
      store['refresh_token'] = 'r1';

      var expired = false;
      final api = ApiClient(
          storage: TokenStorage(), onSessionExpired: () => expired = true);
      api.dio.httpClientAdapter = _FakeAdapter((o) async {
        if (o.path == '/auth/refresh') {
          return _json({'access_token': 'fresh'}, 200);
        }
        // Even the brand-new token is refused: the session was revoked
        // server-side (signed out from another device, account disabled).
        return _json({'detail': 'Unauthorized'}, 401);
      });

      final res = await api.dio.get('/me/subscriptions');
      expect(res.statusCode, 401);
      expect(expired, isTrue);
    });
  });

  // ---------------------------------------------------------------------
  group('session fencing', () {
    test('a wipe racing an in-flight refresh cannot resurrect credentials',
        () async {
      store['access_token'] = 'expired';
      store['refresh_token'] = 'r1';

      final storage = TokenStorage();
      final fence = SessionFence();
      // Open the session at the generation the migrated record will carry.
      fence.open((await storage.readBundle())!.generation);

      final wipe = SessionWipe(fence)
        ..register('credentials', (_) => storage.clear());

      final refreshReached = Completer<void>();
      final releaseRefresh = Completer<void>();
      final api = ApiClient(storage: storage, fence: fence);
      api.dio.httpClientAdapter = _FakeAdapter((o) async {
        if (o.path == '/auth/refresh') {
          refreshReached.complete();
          await releaseRefresh.future;
          // The server happily issues a brand-new, perfectly valid pair.
          return _json({'access_token': 'fresh', 'refresh_token': 'r2'}, 200);
        }
        return _json({'detail': 'Unauthorized'}, 401);
      });

      final call = api.dio.get('/me/subscriptions');
      await refreshReached.future;

      // The user signs out while the refresh is still in the air.
      await wipe.wipe(SessionWipeReason.userSignedOut);
      expect(await storage.readBundle(), isNull);

      releaseRefresh.complete();
      await call;

      expect(await storage.readBundle(), isNull,
          reason: 'a completed refresh must never re-create a wiped session');
      expect(store.values.any((v) => v.contains('fresh')), isFalse);
      expect(store.values.any((v) => v.contains('r2')), isFalse);
    });

    test('storage refuses a write stamped with a superseded generation',
        () async {
      final ts = TokenStorage();
      final stale =
          await ts.beginSession(accessToken: 'a1', refreshToken: 'r1');
      // A second sign-in (another account, or a re-login) supersedes it.
      await ts.beginSession(accessToken: 'a2', refreshToken: 'r2');

      final wrote = await ts.renewSession(
          generation: stale,
          accessToken: 'resurrected',
          refreshToken: 'resurrected-r');

      expect(wrote, isFalse);
      expect((await ts.readBundle())!.accessToken, 'a2',
          reason: 'the newer session must not be overwritten by an older one');
    });

    test('a stale profile write is dropped', () async {
      final ts = TokenStorage();
      final stale = await ts.beginSession(accessToken: 'a1');
      await ts.clear();

      await ts.saveProfile('{"id":"gone"}', generation: stale);
      expect(await ts.readProfile(), isNull,
          reason: 'a wiped device must not keep the previous account\'s PII');
    });

    test('the fence rejects everything once closed', () {
      final fence = SessionFence();
      fence.open(7);
      expect(fence.holds(7), isTrue);
      fence.close();
      expect(fence.holds(7), isFalse);
      expect(fence.holds(0), isFalse);
      expect(fence.holds(null), isFalse);
      fence.open(8);
      expect(fence.holds(7), isFalse, reason: 'generations do not come back');
      expect(fence.holds(8), isTrue);
    });
  });

  // ---------------------------------------------------------------------
  group('scoped, encrypted cache', () {
    late Directory tmp;

    setUp(() async {
      tmp = await Directory.systemTemp.createTemp('scoped_cache_test');
    });

    tearDown(() async {
      if (await tmp.exists()) await tmp.delete(recursive: true);
    });

    test('account B cannot read account A entries', () async {
      const a = MobileDataScope(tenant: 'https://x', principal: 'account-a');
      const b = MobileDataScope(tenant: 'https://x', principal: 'account-b');

      final cache = ResponseCache(directory: tmp)..useScope(a);
      await cache.write('GET /me/balance?', {'balance': 'A-owes-1000'});
      expect(await cache.read('GET /me/balance?'), {'balance': 'A-owes-1000'});

      cache.useScope(b);
      expect(await cache.read('GET /me/balance?'), isNull,
          reason: 'the same request key resolves to a different entry');

      // And B writing its own value cannot clobber or reveal A's.
      await cache.write('GET /me/balance?', {'balance': 'B-owes-2'});
      expect(await cache.read('GET /me/balance?'), {'balance': 'B-owes-2'});
      cache.useScope(a);
      expect(await cache.read('GET /me/balance?'), {'balance': 'A-owes-1000'});
    });

    test('a different TENANT is a different partition too', () async {
      const t1 = MobileDataScope(tenant: 'https://isp-one', principal: 'p');
      const t2 = MobileDataScope(tenant: 'https://isp-two', principal: 'p');

      final cache = ResponseCache(directory: tmp)..useScope(t1);
      await cache.write('GET /me/x?', {'v': 1});
      cache.useScope(t2);
      expect(await cache.read('GET /me/x?'), isNull);
    });

    test('moving a file into another scope does not make it readable',
        () async {
      const a = MobileDataScope(tenant: 'https://x', principal: 'account-a');
      const b = MobileDataScope(tenant: 'https://x', principal: 'account-b');

      final cache = ResponseCache(directory: tmp)..useScope(a);
      await cache.write('GET /me/secret?', {'pii': 'ada@example.com'});
      cache.useScope(b);
      await cache.write('GET /me/secret?', {'pii': 'bob@example.com'});

      final dirA = Directory('${tmp.path}/s_${a.segment}');
      final dirB = Directory('${tmp.path}/s_${b.segment}');
      final stolen = dirA.listSync().whereType<File>().single;
      // An attacker with filesystem access renames A's entry into B's
      // directory, under the name B would look for.
      final target = dirB.listSync().whereType<File>().single;
      final bytes = stolen.readAsBytesSync();
      target.writeAsBytesSync(bytes);

      expect(await cache.read('GET /me/secret?'), isNull,
          reason: 'the scope is in the AEAD tag, not just the path');
    });

    test('deleting the encryption key makes cached data unusable', () async {
      final cipher = CacheCipher();
      final cache = ResponseCache(directory: tmp, cipher: cipher)
        ..useScope(
            const MobileDataScope(tenant: 'https://x', principal: 'acct'));
      await cache.write('GET /me/invoices?', {'total': '42000'});
      expect(await cache.read('GET /me/invoices?'), isNotNull);

      // The file is still there…
      expect(tmp.listSync(recursive: true).whereType<File>(), isNotEmpty);

      // …but the key is gone, and a fresh cipher instance cannot re-derive it.
      await cipher.rotate();
      final reopened = ResponseCache(directory: tmp, cipher: CacheCipher())
        ..useScope(
            const MobileDataScope(tenant: 'https://x', principal: 'acct'));

      expect(await reopened.read('GET /me/invoices?'), isNull,
          reason: 'a destroyed key is a permanent delete, and a miss — not an '
              'error the user sees');
    });

    test('with no key material available, nothing is written at all', () async {
      // No secure-storage channel at all: the cipher cannot obtain a key.
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(storageChannel, null);
      addTearDown(mockSecureStorage);

      final cache = ResponseCache(directory: tmp)
        ..useScope(
            const MobileDataScope(tenant: 'https://x', principal: 'acct'));
      await cache.write('GET /me/x?', {'pii': 'ada@example.com'});

      final written = tmp.listSync(recursive: true).whereType<File>();
      expect(written, isEmpty,
          reason: 'never degrade to a plaintext cache when sealing fails');
    });

    test('legacy plaintext entries are purged on first use', () async {
      // What the previous cache format left behind.
      final legacy = File('${tmp.path}/GET__me_subscriptions_123.json')
        ..writeAsStringSync('{"plan":"Fibre 100","customer":"Ada"}');
      expect(legacy.existsSync(), isTrue);

      final cache = ResponseCache(directory: tmp);
      await cache.read('GET /anything');

      expect(legacy.existsSync(), isFalse,
          reason: 'unreadable is not deleted — the plaintext must actually go');
    });
  });

  // ---------------------------------------------------------------------
  group('wipe coordinator', () {
    test('one call clears every participant and closes the fence', () async {
      final cleared = <String>[];
      final fence = SessionFence()..open(3);
      final wipe = SessionWipe(fence)
        ..register('a', (_) async => cleared.add('a'))
        ..register('b', (_) async => cleared.add('b'))
        ..register('c', (_) async => cleared.add('c'));

      await wipe.wipe(SessionWipeReason.sessionExpired);

      expect(cleared, ['a', 'b', 'c'], reason: 'in registration order');
      expect(fence.isOpen, isFalse);
    });

    test('the fence closes before any participant runs', () async {
      final fence = SessionFence()..open(3);
      var fenceOpenWhenFirstRan = true;
      final wipe = SessionWipe(fence)
        ..register('first', (_) async => fenceOpenWhenFirstRan = fence.isOpen);

      await wipe.wipe(SessionWipeReason.userSignedOut);
      expect(fenceOpenWhenFirstRan, isFalse,
          reason: 'clearing storage while writers are still live just races '
              'them');
    });

    test('a failing participant does not abandon the rest', () async {
      final cleared = <String>[];
      final wipe = SessionWipe(SessionFence()..open(1))
        ..register('boom', (_) async => throw StateError('keystore locked'))
        ..register('after', (_) async => cleared.add('after'));

      await wipe.wipe(SessionWipeReason.credentialsRevoked);
      expect(cleared, ['after']);
    });

    test('concurrent wipes coalesce onto one run', () async {
      var runs = 0;
      final gate = Completer<void>();
      final wipe = SessionWipe(SessionFence()..open(1))
        ..register('slow', (_) async {
          runs++;
          await gate.future;
        });

      final a = wipe.wipe(SessionWipeReason.userSignedOut);
      final b = wipe.wipe(SessionWipeReason.credentialsRevoked);
      gate.complete();
      await Future.wait([a, b]);

      expect(runs, 1);
    });
  });

  // ---------------------------------------------------------------------
  group('AuthController teardown', () {
    ProviderContainer build(TokenStorage ts, _FakeAuthRepository repo,
        {ResponseCache? cache}) {
      final c = ProviderContainer(overrides: [
        tokenStorageProvider.overrideWithValue(ts),
        authRepositoryProvider.overrideWithValue(repo),
        biometricServiceProvider.overrideWithValue(_NoBiometrics()),
        if (cache != null) responseCacheProvider.overrideWithValue(cache)
      ]);
      addTearDown(c.dispose);
      return c;
    }

    test('session expiry clears persisted tokens AND the cached profile',
        () async {
      final ts = TokenStorage();
      await ts.beginSession(accessToken: 'a1', refreshToken: 'r1');
      await ts.saveProfile(jsonEncode(
          Me(id: '1', firstName: 'A', lastName: 'B', email: 'a@b.c').toJson()));

      final c = build(ts, _FakeAuthRepository(ts));
      final n = c.read(authControllerProvider.notifier);
      await n.bootstrap();
      expect(c.read(authControllerProvider).isAuthenticated, isTrue);

      await n.onSessionExpired();

      expect(c.read(authControllerProvider).isAuthenticated, isFalse);
      expect(await ts.readBundle(), isNull, reason: 'tokens gone');
      expect(await ts.readProfile(), isNull, reason: 'cached PII gone too');
    });

    test('explicit sign-out clears the session through the coordinator',
        () async {
      final ts = TokenStorage();
      await ts.beginSession(accessToken: 'a1', refreshToken: 'r1');
      await ts.saveProfile('{"id":"1","first_name":"A","last_name":"B",'
          '"email":"a@b.c"}');
      await ts.setBiometricEnabled(true);

      final repo = _FakeAuthRepository(ts);
      final c = build(ts, repo);
      final n = c.read(authControllerProvider.notifier);
      await n.bootstrap();

      await n.logout();

      expect(repo.logoutCalls, 1, reason: 'the server session is revoked too');
      expect(await ts.readBundle(), isNull);
      expect(await ts.readProfile(), isNull);
      expect(await ts.isBiometricEnabled(), isFalse,
          reason: 'an explicit sign-out resets the device opt-in');
      expect(c.read(authControllerProvider).isAuthenticated, isFalse);
    });

    test('bootstrap keeps the session when the server merely cannot answer',
        () async {
      final ts = TokenStorage();
      await ts.beginSession(accessToken: 'a1', refreshToken: 'r1');
      await ts.saveProfile(jsonEncode(
          Me(id: '1', firstName: 'A', lastName: 'B', email: 'a@b.c').toJson()));

      final repo = _FakeAuthRepository(ts)
        ..meThrows = DioException(
            requestOptions: RequestOptions(path: '/auth/me'),
            type: DioExceptionType.connectionTimeout);
      final c = build(ts, repo);
      await c.read(authControllerProvider.notifier).bootstrap();

      expect(c.read(authControllerProvider).isAuthenticated, isTrue,
          reason: 'offline renders the cached profile optimistically');
      expect(await ts.readBundle(), isNotNull);
    });

    test('bootstrap keeps CREDENTIALS on a transport failure with no profile',
        () async {
      final ts = TokenStorage();
      await ts.beginSession(accessToken: 'a1', refreshToken: 'r1');

      final repo = _FakeAuthRepository(ts)
        ..meThrows = DioException(
            requestOptions: RequestOptions(path: '/auth/me'),
            type: DioExceptionType.connectionError);
      final c = build(ts, repo);
      await c.read(authControllerProvider.notifier).bootstrap();

      // Nothing to render, so the user sees /login — but the session is still
      // on the device and the next successful bootstrap restores it. This used
      // to wipe the tokens outright.
      expect(await ts.readBundle(), isNotNull,
          reason: 'a dropped connection is not a revocation');
    });

    test('bootstrap wipes when the server rejects the session', () async {
      final ts = TokenStorage();
      await ts.beginSession(accessToken: 'a1', refreshToken: 'r1');
      await ts.saveProfile(jsonEncode(
          Me(id: '1', firstName: 'A', lastName: 'B', email: 'a@b.c').toJson()));

      final repo = _FakeAuthRepository(ts)
        ..meThrows = ApiException('Unauthorized', statusCode: 401);
      final c = build(ts, repo);
      await c.read(authControllerProvider.notifier).bootstrap();

      expect(await ts.readBundle(), isNull);
      expect(await ts.readProfile(), isNull);
      expect(c.read(authControllerProvider).isAuthenticated, isFalse);
    });

    test('a resolved session partitions the cache by principal', () async {
      final tmp = await Directory.systemTemp.createTemp('auth_scope_test');
      addTearDown(() async {
        if (await tmp.exists()) await tmp.delete(recursive: true);
      });
      final cache = ResponseCache(directory: tmp);

      final ts = TokenStorage();
      await ts.beginSession(accessToken: 'a1', refreshToken: 'r1');
      final repo = _FakeAuthRepository(ts,
          me0:
              Me(id: 'acct-77', firstName: 'A', lastName: 'B', email: 'a@b.c'));
      final c = build(ts, repo, cache: cache);
      await c.read(authControllerProvider.notifier).bootstrap();

      expect(cache.scope.principal, 'acct-77');
      expect((await ts.readBundle())!.scope.principal, 'acct-77',
          reason: 'the record knows whose tokens it holds on the next launch');
    });
  });

  // ---------------------------------------------------------------------
  group('log redaction', () {
    test('no tokens, auth headers, query secrets or bodies reach a breadcrumb',
        () async {
      final lines = <String>[];
      Log.sink = (message, category, data) {
        lines.add(message);
        if (data != null) lines.addAll(data.values.map((v) => '$v'));
      };

      const jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij';
      store['access_token'] = jwt;
      store['refresh_token'] = 'r3fr3sh-t0k3n-value-here';

      final api = ApiClient(storage: TokenStorage());
      api.dio.httpClientAdapter = _FakeAdapter((o) async {
        if (o.path == '/auth/refresh') {
          return _json({'detail': 'nope'}, 401);
        }
        throw DioException(
            requestOptions: o,
            type: DioExceptionType.badResponse,
            response: Response(
                requestOptions: o,
                statusCode: 500,
                // A response body full of exactly what must not be logged.
                data: {
                  'access_token': jwt,
                  'customer_email': 'ada@example.com',
                  'detail': 'Authorization: Bearer $jwt'
                }));
      });

      // A path carrying an inline query-string secret, and a failure whose
      // DioException stringifies the whole response body.
      try {
        await api.dio.get('/me/thing?token=$jwt&password=hunter2');
      } catch (e) {
        Log.breadcrumb('call failed', data: {'error': Log.describeError(e)});
      }
      Log.breadcrumb('Authorization: Bearer $jwt', category: 'test');
      Log.breadcrumb('body', data: {'raw': '{"access_token":"$jwt"}'});

      final all = lines.join('\n');
      expect(all, isNot(contains(jwt)), reason: 'no token, anywhere');
      expect(all, isNot(contains('eyJhbGciOiJIUzI1NiJ9')));
      expect(all, isNot(contains('hunter2')));
      expect(all, isNot(contains('ada@example.com')),
          reason: 'no response body content');
      expect(all.toLowerCase(), isNot(contains('bearer eyj')));
      // And it is still useful: the shape of the failure survives.
      expect(all, contains('/me/thing'));
      expect(lines.any((l) => l.contains('DioException')), isTrue);
    });

    test('describeError never carries a response body or URL', () {
      final e = DioException(
          requestOptions: RequestOptions(path: '/me/x?token=s3cret'),
          type: DioExceptionType.badResponse,
          response: Response(
              requestOptions: RequestOptions(path: '/me/x'),
              statusCode: 403,
              data: {'detail': 'ada@example.com owes 42000'}));

      final described = Log.describeError(e);
      expect(described, 'DioException(badResponse, 403)');
      expect(described, isNot(contains('ada@example.com')));
      expect(described, isNot(contains('s3cret')));
    });

    test('redact strips the shapes that keep leaking', () {
      // Regression: the header rule used to consume only the word "Bearer",
      // leaving the credential itself in the line.
      expect(Log.redact('Authorization: Bearer abc.def.ghi'),
          'Authorization: [redacted]');
      expect(
          Log.redact('authorization=abc.def.ghi'), 'authorization=[redacted]');
      expect(Log.redact('{"authorization":"Bearer abc.def.ghi","n":1}'),
          isNot(contains('abc.def.ghi')));
      expect(Log.redact('Bearer abc.def.ghi'), isNot(contains('abc.def.ghi')));
      expect(Log.redact('GET /x?refresh_token=abcdef123456'),
          isNot(contains('abcdef123456')));
      expect(Log.redact('{"password":"hunter2"}'), isNot(contains('hunter2')));
      // And it leaves the diagnostic part alone.
      expect(Log.redact('GET /me/subscriptions'), 'GET /me/subscriptions');
    });
  });
}
