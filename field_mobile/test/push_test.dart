import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/push/push_registrar.dart';
import 'package:dotmac_field/core/push/push_source.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fake_http.dart';

class _AuthedController extends AuthController {
  @override
  AuthState build() => const Authenticated(LoginMode.staff);
}

class _UnauthedController extends AuthController {
  @override
  AuthState build() => const Unauthenticated();
}

void main() {
  late FakeHttpAdapter adapter;
  late FakePushSource push;
  late List<String> deepLinks;

  final freshToken = fakeJwt(
    expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
  );

  ProviderContainer makeContainer({required bool authenticated}) {
    adapter = FakeHttpAdapter();
    push = FakePushSource(initialToken: 'fcm-token-1');
    deepLinks = [];

    final store = InMemoryTokenStore();
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'));
    dio.httpClientAdapter = adapter;
    final client = ApiClient(
      baseUrl: 'https://test.local',
      tokenStore: store,
      dio: dio,
    );
    store.save(
      accessToken: freshToken,
      refreshToken: 'r',
      loginMode: LoginMode.staff,
    );

    return ProviderContainer(
      overrides: [
        apiClientProvider.overrideWithValue(client),
        pushSourceProvider.overrideWithValue(push),
        authControllerProvider.overrideWith(
          authenticated ? _AuthedController.new : _UnauthedController.new,
        ),
      ],
    );
  }

  test('registers the device token when authenticated', () async {
    final container = makeContainer(authenticated: true);
    addTearDown(container.dispose);
    var registrations = 0;
    adapter.on('POST', '/api/v1/field/devices', (options) {
      registrations++;
      final body = options.data as Map;
      expect(body['fcm_token'], 'fcm-token-1');
      expect(body['platform'], isNotEmpty);
      expect(body['app_version'], isNotEmpty);
      return (201, {'id': 'dev-1'});
    });

    final registrar = PushRegistrar(
      _RefAdapter(container),
      onDeepLink: deepLinks.add,
    );
    expect(await registrar.registerToken(), isTrue);
    expect(registrations, 1);
  });

  test('does not register when unauthenticated', () async {
    final container = makeContainer(authenticated: false);
    addTearDown(container.dispose);
    final registrar = PushRegistrar(
      _RefAdapter(container),
      onDeepLink: deepLinks.add,
    );
    expect(await registrar.registerToken(), isFalse);
  });

  test('token rotation re-registers', () async {
    final container = makeContainer(authenticated: true);
    addTearDown(container.dispose);
    final tokens = <String>[];
    adapter.on('POST', '/api/v1/field/devices', (options) {
      tokens.add((options.data as Map)['fcm_token'] as String);
      return (201, {'id': 'dev-1'});
    });

    final registrar = PushRegistrar(
      _RefAdapter(container),
      onDeepLink: deepLinks.add,
    )..start();
    push.rotateToken('fcm-token-2');
    await Future<void>.delayed(const Duration(milliseconds: 20));
    expect(tokens, ['fcm-token-1', 'fcm-token-2']);
    await registrar.dispose();
  });

  test('notification tap deep-links to the job', () async {
    final container = makeContainer(authenticated: true);
    addTearDown(container.dispose);
    final registrar = PushRegistrar(
      _RefAdapter(container),
      onDeepLink: deepLinks.add,
    )..start();

    push.emit(
      const PushMessage(
        fromTap: true,
        data: {'type': 'work_order_assigned', 'work_order_id': 'wo-9'},
      ),
    );
    push.emit(
      const PushMessage(
        fromTap: true,
        data: {'type': 'work_order_comment', 'work_order_id': 'wo-10'},
      ),
    );
    push.emit(
      const PushMessage(
        fromTap: false,
        data: {'type': 'work_order_assigned', 'work_order_id': 'x'},
      ),
    );
    push.emit(const PushMessage(fromTap: true, data: {'type': 'unknown'}));
    await Future<void>.delayed(const Duration(milliseconds: 20));

    expect(deepLinks, ['/jobs/wo-9', '/jobs/wo-10']);
    await registrar.dispose();
  });

  test(
    'foreground messages call foreground handler without navigation',
    () async {
      final container = makeContainer(authenticated: true);
      addTearDown(container.dispose);
      final foreground = <String?>[];
      final registrar = PushRegistrar(
        _RefAdapter(container),
        onDeepLink: deepLinks.add,
        onForegroundMessage: (message, route) => foreground.add(route),
      )..start();

      push.emit(
        const PushMessage(
          fromTap: false,
          data: {'type': 'work_order_comment', 'work_order_id': 'wo-11'},
        ),
      );
      await Future<void>.delayed(const Duration(milliseconds: 20));

      expect(deepLinks, isEmpty);
      expect(foreground, ['/jobs/wo-11']);
      await registrar.dispose();
    },
  );

  test('routeForMessage mapping', () {
    expect(
      routeForMessage({'type': 'work_order_assigned', 'work_order_id': 'a'}),
      '/jobs/a',
    );
    expect(
      routeForMessage({'type': 'work_order_comment', 'work_order_id': 'b'}),
      '/jobs/b',
    );
    expect(routeForMessage({'type': 'other'}), isNull);
    expect(routeForMessage({}), isNull);
  });
}

/// PushRegistrar needs a Ref; ProviderContainer satisfies the same read/listen
/// surface via this thin adapter.
class _RefAdapter implements Ref {
  _RefAdapter(this.providerContainer);

  final ProviderContainer providerContainer;

  @override
  T read<T>(ProviderListenable<T> provider) => providerContainer.read(provider);

  @override
  ProviderSubscription<T> listen<T>(
    ProviderListenable<T> provider,
    void Function(T? previous, T next) listener, {
    void Function(Object error, StackTrace stackTrace)? onError,
    bool fireImmediately = false,
  }) => providerContainer.listen(
    provider,
    listener,
    onError: onError,
    fireImmediately: fireImmediately,
  );

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}
