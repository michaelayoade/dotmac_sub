import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/providers/data_providers.dart'
    show accountIdProvider, cacheFor;

// A controllable stand-in for the signed-in identity.
final _testId = StateProvider<String?>((ref) => null);

// A cached provider that records how many times it (re)built.
var _builds = 0;
final _probe = FutureProvider.autoDispose<int>((ref) async {
  cacheFor(ref);
  return ++_builds;
});

void main() {
  // Regression: data providers are kept alive by cacheFor's 5-min TTL. Without
  // binding to the identity, an in-app session expiry → re-login served the
  // previous session's cached value and never refetched (the "no data after
  // login" bug). cacheFor now watches accountIdProvider, so a change in the
  // signed-in user invalidates every cached provider.
  test('cacheFor refetches when the account id changes', () async {
    _builds = 0;
    final c = ProviderContainer(
      overrides: [accountIdProvider.overrideWith((ref) => ref.watch(_testId))],
    );
    addTearDown(c.dispose);

    c.read(_testId.notifier).state = 'user-a';
    expect(await c.read(_probe.future), 1);

    // Same identity → still cached (kept alive), no rebuild.
    c.read(_testId.notifier).state = 'user-a';
    expect(await c.read(_probe.future), 1);

    // New identity (re-login / account switch) → cached value is invalidated
    // and the provider refetches.
    c.read(_testId.notifier).state = 'user-b';
    await Future<void>.delayed(Duration.zero);
    expect(await c.read(_probe.future), 2);
  });
}
