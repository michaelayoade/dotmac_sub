import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/secure/offline_wipe.dart';
import 'package:dotmac_field/core/secure/secure_field_store.dart';
import 'package:dotmac_field/core/secure/session_lifecycle.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fake_http.dart';
import 'helpers/secure_store.dart';

/// ADR-0067 § 4 — *transport failure is not revocation* — at the one place the
/// audit measured it violated (finding R2, `auth_state.dart:82-86` at the
/// audited revision): the cold-start restore.
///
/// A technician launching the app in a coverage dead zone holds a refresh
/// credential good for thirty days. Nothing on the device can tell whether the
/// server would still honour it, so nothing on the device is entitled to
/// destroy it. Only an authoritative refusal on the refresh exchange itself,
/// or a credential that is provably unusable without asking anyone, ends the
/// session — and when one does, it ends through the same single wipe an
/// explicit logout uses.
void main() {
  useHostSqlite3();

  final expiring = fakeJwt(
    expiry: DateTime.now().toUtc().add(const Duration(seconds: 10)),
  );
  final fresh = fakeJwt(
    expiry: DateTime.now().toUtc().add(const Duration(minutes: 30)),
  );

  ApiClient client(
    HttpClientAdapter adapter,
    TokenStore store, {
    void Function()? onExpired,
  }) {
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'))
      ..httpClientAdapter = adapter;
    final refreshDio = Dio(BaseOptions(baseUrl: 'https://test.local'))
      ..httpClientAdapter = adapter;
    return ApiClient(
      baseUrl: 'https://test.local',
      tokenStore: store,
      dio: dio,
      refreshDio: refreshDio,
      onSessionExpired: onExpired,
    );
  }

  ProviderContainer containerFor(
    TokenStore store,
    ApiClient api, {
    SessionLifecycle? session,
  }) => ProviderContainer(
    overrides: [
      tokenStoreProvider.overrideWithValue(store),
      apiClientProvider.overrideWithValue(api),
      if (session != null) sessionLifecycleProvider.overrideWithValue(session),
    ],
  );

  /// One launch of the app over storage that already exists, torn down at the
  /// end. Calling it twice over the same [store] is a process restart: nothing
  /// in memory survives, only what was written to the device.
  Future<AuthState> coldStart(
    TokenStore store,
    HttpClientAdapter adapter, {
    SessionLifecycle? session,
  }) async {
    final container = containerFor(
      store,
      client(adapter, store),
      session: session,
    );
    try {
      container.read(authControllerProvider);
      await pumpEventQueue();
      return container.read(authControllerProvider);
    } finally {
      container.dispose();
    }
  }

  Future<InMemoryTokenStore> signedInStore() async {
    final store = InMemoryTokenStore();
    await store.save(
      accessToken: expiring,
      refreshToken: 'refresh-thirty-days',
      loginMode: LoginMode.staff,
    );
    return store;
  }

  FakeHttpAdapter refreshReturning(int status, Object body) =>
      FakeHttpAdapter()
        ..on('POST', '/api/v1/auth/refresh', (_) => (status, body));

  group('a restore that reaches nothing keeps the session', () {
    final unreachable = <String, HttpClientAdapter Function()>{
      'DNS failure': () => _UnreachableAdapter(
        DioExceptionType.connectionError,
        error: const SocketException('Failed host lookup: selfcare.dotmac.io'),
      ),
      'connection timeout': () =>
          _UnreachableAdapter(DioExceptionType.connectionTimeout),
      'receive timeout': () =>
          _UnreachableAdapter(DioExceptionType.receiveTimeout),
      'backend 500': () => refreshReturning(500, {'detail': 'boom'}),
      'backend 503': () => refreshReturning(503, {'detail': 'draining'}),
    };

    for (final entry in unreachable.entries) {
      test('${entry.key} leaves an offline session, not a logout', () async {
        final store = await signedInStore();
        var expired = false;
        final container = containerFor(
          store,
          client(entry.value(), store, onExpired: () => expired = true),
        );
        addTearDown(container.dispose);

        container.read(authControllerProvider);
        await pumpEventQueue();

        final state = container.read(authControllerProvider);
        expect(state, isA<Authenticated>());
        expect((state as Authenticated).isOffline, isTrue);
        expect(state.mode, LoginMode.staff);

        // The whole point: the thirty-day credential is still on the handset.
        expect(await store.refreshToken, 'refresh-thirty-days');
        expect(await store.accessToken, expiring);
        expect(await store.loginMode, LoginMode.staff);
        expect(expired, isFalse);
      });
    }
  });

  test(
    'an offline restore is retryable, and retrying is not destructive',
    () async {
      final store = await signedInStore();
      final container = containerFor(
        store,
        client(_UnreachableAdapter(DioExceptionType.connectionError), store),
      );
      addTearDown(container.dispose);

      container.read(authControllerProvider);
      await pumpEventQueue();
      expect(
        (container.read(authControllerProvider) as Authenticated).isOffline,
        isTrue,
      );

      // Still no coverage: a retry that fails the same way must be as harmless
      // as the first attempt was.
      await container.read(authControllerProvider.notifier).retryRestore();
      expect(
        (container.read(authControllerProvider) as Authenticated).isOffline,
        isTrue,
      );
      expect(await store.refreshToken, 'refresh-thirty-days');
    },
  );

  test(
    'a 401 on the refresh exchange ends the session through the one wipe',
    () async {
      final device = newTestDevice();
      await device.tokenStore.save(
        accessToken: expiring,
        refreshToken: 'refresh-dead',
        loginMode: LoginMode.staff,
      );
      final releaseWipe = Completer<void>();
      addTearDown(() {
        if (!releaseWipe.isCompleted) releaseWipe.complete();
      });
      final recording = _RecordingWipe(
        device.wipe,
        waitUntil: releaseWipe.future,
      );
      final session = device.lifecycle(wipeOverride: recording);
      await session.restore();
      expect(session.store, isNotNull);

      late final ProviderContainer container;
      final api = client(
        refreshReturning(401, {'detail': 'Account disabled'}),
        device.tokenStore,
        // The production wiring: the API client reports an authoritative refusal
        // and the controller owns what happens next.
        onExpired: () => unawaited(
          container.read(authControllerProvider.notifier).sessionExpired(),
        ),
      );
      container = containerFor(device.tokenStore, api, session: session);
      addTearDown(container.dispose);

      // Await the state owner's semantic completion signal, not an arbitrary
      // number of event-loop turns. CI is slow enough for the credential clear
      // to finish before the store close, which made the old pumpEventQueue()
      // observation race the wipe it was trying to prove.
      final terminal = Completer<Unauthenticated>();
      final subscription = container.listen<AuthState>(authControllerProvider, (
        _,
        next,
      ) {
        if (next is Unauthenticated && !terminal.isCompleted) {
          terminal.complete(next);
        }
      }, fireImmediately: true);
      addTearDown(subscription.close);

      // A deliberately parked wipe is the canary: the controller must not
      // publish Unauthenticated while principal-scoped storage is still open.
      await recording.started.timeout(const Duration(seconds: 2));
      expect(container.read(authControllerProvider), isA<RestoringSession>());
      expect(session.store, isNotNull);
      expect(terminal.isCompleted, isFalse);

      releaseWipe.complete();
      final state = await terminal.future.timeout(const Duration(seconds: 2));

      // Exactly one wipe, with the revocation trigger. Two would mean the
      // restore path had grown a second ending of its own.
      expect(recording.requests.map((request) => request.trigger), [
        WipeTrigger.tokenRevoked,
      ]);
      expect(await device.tokenStore.accessToken, isNull);
      expect(await device.tokenStore.refreshToken, isNull);
      expect(await device.tokenStore.loginMode, isNull);
      expect(session.store, isNull);

      expect(identical(container.read(authControllerProvider), state), isTrue);
      expect(state.error, isNotNull);
    },
  );

  test('a 401 ends the session even with no expiry callback wired', () async {
    // The restore path must be able to reach the ending on its own, rather
    // than depending on a fire-and-forget callback having got there first.
    final store = await signedInStore();
    final container = containerFor(
      store,
      client(refreshReturning(401, {'detail': 'Account disabled'}), store),
    );
    addTearDown(container.dispose);

    container.read(authControllerProvider);
    await pumpEventQueue();

    expect(container.read(authControllerProvider), isA<Unauthenticated>());
    expect(await store.refreshToken, isNull);
    expect(await store.loginMode, isNull);
  });

  test(
    'a login mode left behind with no credential is terminal, not offline',
    () async {
      // Half a credential record is a state no reader has a correct branch for
      // (ADR-0067 § 1). It is provably unusable without asking anyone, so it is
      // an ending — and it must not be mistaken for a coverage problem.
      final store = _ModeOnlyTokenStore();
      await store.save(
        accessToken: expiring,
        refreshToken: 'r',
        loginMode: LoginMode.staff,
      );
      final container = containerFor(store, client(FakeHttpAdapter(), store));
      addTearDown(container.dispose);

      container.read(authControllerProvider);
      await pumpEventQueue();

      expect(container.read(authControllerProvider), isA<Unauthenticated>());
    },
  );

  test(
    'a logout during an in-flight refresh is not undone when it returns',
    () async {
      final gate = Completer<void>();
      final adapter = _GatedAdapter(gate, (
        200,
        {'access_token': fresh, 'refresh_token': 'refresh-rotated'},
      ));
      final store = await signedInStore();
      final container = containerFor(store, client(adapter, store));
      addTearDown(container.dispose);

      container.read(authControllerProvider);
      await pumpEventQueue();
      // The cold-start restore is parked inside the refresh exchange.
      expect(adapter.started, 1);
      expect(container.read(authControllerProvider), isA<RestoringSession>());

      await container.read(authControllerProvider.notifier).logout();
      expect(container.read(authControllerProvider), isA<Unauthenticated>());

      // The exchange now succeeds, far too late. Its rotated credential belongs
      // to a session that is over: it may not be written back onto a handset
      // that has just been wiped, and it may not republish an authenticated
      // state over the ending.
      gate.complete();
      await pumpEventQueue();

      expect(container.read(authControllerProvider), isA<Unauthenticated>());
      expect(await store.accessToken, isNull);
      expect(await store.refreshToken, isNull);
      expect(await store.loginMode, isNull);
    },
  );

  group('process restart', () {
    test('after an offline restore, the credential is still there', () async {
      final store = await signedInStore();

      final first = await coldStart(
        store,
        _UnreachableAdapter(DioExceptionType.connectionError),
      );
      expect((first as Authenticated).isOffline, isTrue);

      // Killed and relaunched, still with no coverage.
      final second = await coldStart(
        store,
        _UnreachableAdapter(DioExceptionType.connectionError),
      );
      expect((second as Authenticated).isOffline, isTrue);
      expect(await store.refreshToken, 'refresh-thirty-days');

      // Relaunched once more, this time in coverage. The stored credential is
      // the whole of what was needed to come back online — no re-login, and
      // under federated login no round trip to the identity provider.
      final third = await coldStart(
        store,
        refreshReturning(200, {
          'access_token': fresh,
          'refresh_token': 'refresh-rotated',
        }),
      );
      expect((third as Authenticated).isOffline, isFalse);
      expect(await store.accessToken, fresh);
      expect(await store.refreshToken, 'refresh-rotated');
    });

    test(
      'after a backend outage, the session comes back on recovery',
      () async {
        final store = await signedInStore();

        final duringOutage = await coldStart(store, refreshReturning(500, {}));
        expect((duringOutage as Authenticated).isOffline, isTrue);

        final afterOutage = await coldStart(
          store,
          refreshReturning(200, {'access_token': fresh}),
        );
        expect((afterOutage as Authenticated).isOffline, isFalse);
      },
    );

    test('after a terminal refusal, the app comes back signed out', () async {
      final store = await signedInStore();

      final refused = await coldStart(
        store,
        refreshReturning(401, {'detail': 'Account disabled'}),
      );
      expect(refused, isA<Unauthenticated>());

      // Nothing was left on the device for the next launch to restore from.
      final relaunched = await coldStart(store, FakeHttpAdapter());
      expect(relaunched, isA<Unauthenticated>());
      expect((relaunched as Unauthenticated).error, isNull);
    });

    test(
      'after a logout that raced a refresh, the app comes back signed out',
      () async {
        final gate = Completer<void>();
        final adapter = _GatedAdapter(gate, (
          200,
          {'access_token': fresh, 'refresh_token': 'refresh-rotated'},
        ));
        final store = await signedInStore();
        final container = containerFor(store, client(adapter, store));

        container.read(authControllerProvider);
        await pumpEventQueue();
        await container.read(authControllerProvider.notifier).logout();
        gate.complete();
        await pumpEventQueue();
        container.dispose();

        final relaunched = await coldStart(store, FakeHttpAdapter());
        expect(relaunched, isA<Unauthenticated>());
      },
    );
  });

  group('the refresh outcome tells transport apart from refusal', () {
    test('a transport failure is unreachable, never refused', () async {
      final store = await signedInStore();
      expect(
        await client(
          _UnreachableAdapter(DioExceptionType.connectionError),
          store,
        ).ensureFreshSession(),
        isA<SessionUnreachable>(),
      );
      expect(
        await client(refreshReturning(503, {}), store).ensureFreshSession(),
        isA<SessionUnreachable>(),
      );
      expect(
        await client(refreshReturning(500, {}), store).ensureFreshSession(),
        isA<SessionUnreachable>(),
      );
      expect(await store.refreshToken, 'refresh-thirty-days');
    });

    test('an authoritative refusal on the exchange is refused', () async {
      final store = await signedInStore();
      expect(
        await client(refreshReturning(401, {}), store).ensureFreshSession(),
        isA<SessionRefused>(),
      );
      expect(
        await client(refreshReturning(403, {}), store).ensureFreshSession(),
        isA<SessionRefused>(),
      );
      // The API client reports; it never destroys. The wipe is the owner's.
      expect(await store.refreshToken, 'refresh-thirty-days');
    });

    test('no stored credential at all is absent, not a failure', () async {
      expect(
        await client(
          FakeHttpAdapter(),
          InMemoryTokenStore(),
        ).ensureFreshSession(),
        isA<SessionAbsent>(),
      );
    });

    test('a stale exchange does not write its result back', () async {
      final gate = Completer<void>();
      final store = await signedInStore();
      final api = client(
        _GatedAdapter(gate, (
          200,
          {'access_token': fresh, 'refresh_token': 'refresh-rotated'},
        )),
        store,
      );

      final pending = api.ensureFreshSession();
      await pumpEventQueue();
      api.abandonSession();
      gate.complete();

      expect(await pending, isA<SessionAbsent>());
      expect(await store.accessToken, expiring);
      expect(await store.refreshToken, 'refresh-thirty-days');
    });
  });
}

/// A handset with no route to the server: no response, no status, just a
/// transport error — the shape a DNS failure, a dropped connection and a
/// timeout all arrive in.
class _UnreachableAdapter implements HttpClientAdapter {
  _UnreachableAdapter(this.type, {this.error});

  final DioExceptionType type;
  final Object? error;
  int attempts = 0;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    attempts++;
    await requestStream?.drain<void>();
    throw DioException(requestOptions: options, type: type, error: error);
  }

  @override
  void close({bool force = false}) {}
}

/// An exchange held open until the test releases it, so a logout can be made
/// to land in the middle of one.
class _GatedAdapter implements HttpClientAdapter {
  _GatedAdapter(this.gate, this.response);

  final Completer<void> gate;
  final (int, Object) response;
  int started = 0;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    started++;
    await requestStream?.drain<void>();
    await gate.future;
    final (status, body) = response;
    return ResponseBody.fromString(
      jsonEncode(body),
      status,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

/// A credential record torn in half: the login mode survived a write the
/// tokens did not.
class _ModeOnlyTokenStore extends InMemoryTokenStore {
  @override
  Future<String?> get accessToken async => null;

  @override
  Future<String?> get refreshToken async => null;
}

class _RecordingWipe implements OfflineWipe {
  _RecordingWipe(this.delegate, {required this.waitUntil});

  final OfflineWipe delegate;
  final Future<void> waitUntil;
  final List<WipeRequest> requests = [];
  final Completer<WipeRequest> _started = Completer<WipeRequest>();

  Future<WipeRequest> get started => _started.future;

  @override
  Future<void> wipe(WipeRequest request, {SecureFieldStore? live}) async {
    requests.add(request);
    if (!_started.isCompleted) _started.complete(request);
    await waitUntil;
    await delegate.wipe(request, live: live);
  }

  @override
  Future<List<WipeRequest>> resumeInterrupted() => delegate.resumeInterrupted();
}
