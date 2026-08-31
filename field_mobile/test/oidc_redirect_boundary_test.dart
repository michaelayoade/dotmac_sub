import 'package:dotmac_field/core/deeplink/oidc_redirect.dart';
import 'package:dotmac_field/features/auth/oidc_callback_coordinator.dart';
import 'package:flutter_test/flutter_test.dart';

/// The one boundary the whole OIDC ceremony comes back through. Every test here
/// is about a link that is NOT it: the value of an exact match is entirely in
/// what it refuses.
void main() {
  final config = OidcRedirectConfig.parse(
    origin: kOidcCallbackOriginDefault,
    path: kOidcCallbackPathDefault,
  );

  OidcCallbackCoordinator coordinatorWithCeremony({String state = 'st-1'}) {
    final coordinator = OidcCallbackCoordinator(config: config);
    coordinator.begin(state: state, codeVerifier: 'verifier-1');
    return coordinator;
  }

  Uri callback({String query = ''}) =>
      Uri.parse('$kOidcCallbackOriginDefault$kOidcCallbackPathDefault$query');

  group('the configured boundary', () {
    test('is the exact https origin and path the app ships with', () {
      expect(
        config.redirectUri.toString(),
        'https://links.dotmac.io/oidc/field/callback',
      );
      expect(config.host, 'links.dotmac.io');
      expect(config.path, '/oidc/field/callback');
    });

    test('the build-time knobs default to that boundary', () {
      expect(oidcCallbackOrigin, kOidcCallbackOriginDefault);
      expect(oidcCallbackPath, kOidcCallbackPathDefault);
      expect(OidcRedirectConfig.fromBuild.redirectUri, config.redirectUri);
    });

    test('refuses a custom scheme outright', () {
      expect(
        () => OidcRedirectConfig.parse(
          origin: 'io.dotmac.field://auth',
          path: kOidcCallbackPathDefault,
        ),
        throwsArgumentError,
      );
    });

    test('refuses a wildcard host and a wildcard path', () {
      expect(
        () => OidcRedirectConfig.parse(
          origin: 'https://*.dotmac.io',
          path: kOidcCallbackPathDefault,
        ),
        throwsArgumentError,
      );
      expect(
        () => OidcRedirectConfig.parse(
          origin: kOidcCallbackOriginDefault,
          path: '/oidc/field/*',
        ),
        throwsArgumentError,
      );
    });

    test('refuses a path that is really a prefix', () {
      expect(
        () => OidcRedirectConfig.parse(
          origin: kOidcCallbackOriginDefault,
          path: '/oidc/field/callback/',
        ),
        throwsArgumentError,
      );
    });
  });

  group('exact matching', () {
    test('accepts the callback URL, with and without a query', () {
      expect(config.matches(callback()), isTrue);
      expect(config.matches(callback(query: '?code=abc&state=st-1')), isTrue);
    });

    test('rejects a wrong path', () {
      for (final wrong in <String>[
        '/oidc/field/callback2',
        '/oidc/field/callback/extra',
        '/oidc/field',
        '/',
        '/OIDC/FIELD/CALLBACK',
      ]) {
        expect(
          config.matches(Uri.parse('$kOidcCallbackOriginDefault$wrong')),
          isFalse,
          reason: '$wrong is not the callback path',
        );
      }
    });

    test('rejects a wrong host', () {
      for (final wrong in <String>[
        'https://links.dotmac.io.evil.example',
        'https://evil.example',
        'https://dotmac.io',
        'https://sub.links.dotmac.io',
        'https://selfcare.dotmac.io',
        'https://links.dotmac.io:8443',
        'https://user@links.dotmac.io',
      ]) {
        expect(
          config.matches(Uri.parse('$wrong$kOidcCallbackPathDefault')),
          isFalse,
          reason: '$wrong is not the callback origin',
        );
      }
    });

    test('rejects a non-https scheme on the right host and path', () {
      expect(
        config.matches(
          Uri.parse('http://links.dotmac.io$kOidcCallbackPathDefault'),
        ),
        isFalse,
      );
      expect(
        config.matches(
          Uri.parse(
            'io.dotmac.field://links.dotmac.io$kOidcCallbackPathDefault',
          ),
        ),
        isFalse,
      );
    });
  });

  group('platform route anchoring', () {
    test('anchors the bare callback route onto the verified origin', () {
      final anchored = config.anchor(
        Uri.parse('$kOidcCallbackPathDefault?code=abc&state=st-1'),
      );
      expect(anchored, isNotNull);
      expect(anchored!.host, 'links.dotmac.io');
      expect(anchored.queryParameters['code'], 'abc');
    });

    test('refuses a route that carries its own authority', () {
      // `//evil.example/oidc/field/callback` parses as a scheme-relative URL.
      // Anchoring it naively would move the host; this is the shape that must
      // never be accepted.
      expect(
        config.anchor(Uri.parse('//evil.example$kOidcCallbackPathDefault')),
        isNull,
      );
      expect(
        config.anchor(
          Uri.parse('https://evil.example$kOidcCallbackPathDefault'),
        ),
        isNull,
      );
    });

    test('refuses any other route', () {
      expect(config.anchor(Uri.parse('/today')), isNull);
      expect(config.anchor(Uri.parse('/oidc/field/callback/extra')), isNull);
      expect(config.anchor(Uri.parse('oidc/field/callback')), isNull);
    });
  });

  group('the coordinator', () {
    test('accepts a callback that answers the ceremony in flight', () async {
      final coordinator = coordinatorWithCeremony();
      final result = coordinator.submit(
        callback(query: '?code=the-code&state=st-1'),
      );
      expect(result.outcome, OidcCallbackOutcome.accepted);
      expect(result.code, 'the-code');
      expect(coordinator.hasCeremonyInFlight, isFalse);
    });

    test(
      'DISCARDS a callback with query parameters when no ceremony is in flight',
      () {
        final coordinator = OidcCallbackCoordinator(config: config);
        expect(coordinator.hasCeremonyInFlight, isFalse);

        final result = coordinator.submit(
          callback(query: '?code=injected&state=whatever'),
        );

        expect(result.outcome, OidcCallbackOutcome.noActiveCeremony);
        // Nothing was taken out of the link.
        expect(result.code, isNull);
        expect(result.state, isNull);
      },
    );

    test('discards a replay of an already-accepted callback', () {
      final coordinator = coordinatorWithCeremony();
      final link = callback(query: '?code=the-code&state=st-1');
      expect(coordinator.submit(link).outcome, OidcCallbackOutcome.accepted);
      expect(
        coordinator.submit(link).outcome,
        OidcCallbackOutcome.noActiveCeremony,
      );
    });

    test('discards a callback that arrives after the ceremony expired', () {
      var now = DateTime(2026, 1, 1, 12);
      final coordinator = OidcCallbackCoordinator(
        config: config,
        ttl: const Duration(minutes: 5),
        clock: () => now,
      );
      coordinator.begin(state: 'st-1', codeVerifier: 'v');
      now = now.add(const Duration(minutes: 6));

      expect(
        coordinator.submit(callback(query: '?code=c&state=st-1')).outcome,
        OidcCallbackOutcome.noActiveCeremony,
      );
    });

    test('rejects a mismatched state WITHOUT consuming the ceremony', () {
      final coordinator = coordinatorWithCeremony();
      final result = coordinator.submit(
        callback(query: '?code=c&state=not-ours'),
      );
      expect(result.outcome, OidcCallbackOutcome.stateMismatch);
      expect(result.code, isNull);
      // Anyone able to open the callback URL could otherwise cancel a real
      // sign-in just by hitting it with a wrong state.
      expect(coordinator.hasCeremonyInFlight, isTrue);
    });

    test('rejects a callback carrying no state at all', () {
      final coordinator = coordinatorWithCeremony();
      expect(
        coordinator.submit(callback(query: '?code=c')).outcome,
        OidcCallbackOutcome.stateMismatch,
      );
    });

    test('leaves the ceremony alone for a matching but code-less callback', () {
      final coordinator = coordinatorWithCeremony();
      expect(
        coordinator.submit(callback(query: '?state=st-1')).outcome,
        OidcCallbackOutcome.malformed,
      );
      expect(coordinator.hasCeremonyInFlight, isTrue);
    });

    test('ends the ceremony on a provider error', () async {
      final coordinator = OidcCallbackCoordinator(config: config);
      final ceremony = coordinator.begin(state: 'st-1', codeVerifier: 'v');
      final result = coordinator.submit(
        callback(query: '?error=access_denied&state=st-1'),
      );
      expect(result.outcome, OidcCallbackOutcome.providerError);
      expect(result.error, 'access_denied');
      expect(
        (await ceremony.completion).outcome,
        OidcCallbackOutcome.providerError,
      );
      expect(coordinator.hasCeremonyInFlight, isFalse);
    });

    test('ignores a wrong host and a wrong path even mid-ceremony', () {
      final coordinator = coordinatorWithCeremony();
      for (final hostile in <String>[
        'https://evil.example/oidc/field/callback?code=c&state=st-1',
        'https://links.dotmac.io.evil.example/oidc/field/callback?code=c&state=st-1',
        'https://links.dotmac.io/oidc/field/callback/extra?code=c&state=st-1',
        'https://links.dotmac.io/?code=c&state=st-1',
        'io.dotmac.field://callback?code=c&state=st-1',
      ]) {
        expect(
          coordinator.submit(Uri.parse(hostile)).outcome,
          OidcCallbackOutcome.notOurRedirect,
          reason: hostile,
        );
      }
      // None of them touched the real ceremony.
      expect(coordinator.hasCeremonyInFlight, isTrue);
      expect(
        coordinator.submit(callback(query: '?code=real&state=st-1')).code,
        'real',
      );
    });

    test('ownsLocation is true only for the exact callback route', () {
      final coordinator = OidcCallbackCoordinator(config: config);
      expect(
        coordinator.ownsLocation(Uri.parse(kOidcCallbackPathDefault)),
        isTrue,
      );
      expect(
        coordinator.ownsLocation(Uri.parse('$kOidcCallbackPathDefault?code=c')),
        isTrue,
      );
      expect(coordinator.ownsLocation(Uri.parse('/today')), isFalse);
      expect(
        coordinator.ownsLocation(Uri.parse('/oidc/field/callback/extra')),
        isFalse,
      );
      expect(
        coordinator.ownsLocation(
          Uri.parse('//evil.example$kOidcCallbackPathDefault'),
        ),
        isFalse,
      );
    });

    test('submitLocation routes the platform route through the same door', () {
      final coordinator = coordinatorWithCeremony();
      final result = coordinator.submitLocation(
        Uri.parse('$kOidcCallbackPathDefault?code=abc&state=st-1'),
      );
      expect(result.outcome, OidcCallbackOutcome.accepted);
      expect(result.code, 'abc');
    });

    test('starting a new ceremony abandons the previous one', () async {
      final coordinator = OidcCallbackCoordinator(config: config);
      final first = coordinator.begin(state: 'st-1', codeVerifier: 'v1');
      coordinator.begin(state: 'st-2', codeVerifier: 'v2');
      expect(
        (await first.completion).outcome,
        OidcCallbackOutcome.noActiveCeremony,
      );
      expect(
        coordinator.submit(callback(query: '?code=c&state=st-1')).outcome,
        OidcCallbackOutcome.stateMismatch,
      );
    });
  });
}
