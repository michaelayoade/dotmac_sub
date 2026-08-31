import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/deeplink/oidc_redirect.dart';

/// What the app did with an inbound link that arrived on the verified redirect
/// boundary.
enum OidcCallbackOutcome {
  /// Not this app's redirect boundary at all — wrong host, wrong path, wrong
  /// scheme, or a route carrying its own authority. Nothing was read from it.
  notOurRedirect,

  /// The boundary is ours, but NO ceremony is in flight (never started, already
  /// finished, or expired). The link is DISCARDED without its query parameters
  /// being interpreted. An unsolicited `?code=...` is exactly the shape an
  /// attacker uses to inject their own authorization code, so "no ceremony" is
  /// a refusal, not a reason to go looking for a ceremony to attach it to.
  noActiveCeremony,

  /// A ceremony is in flight but the returned `state` is not its `state`. The
  /// in-flight ceremony is deliberately LEFT ALONE: consuming it here would let
  /// anyone who can open the callback URL cancel a legitimate sign-in.
  stateMismatch,

  /// The provider returned an `error` for this ceremony. The ceremony ends.
  providerError,

  /// Ours, matching, but carrying no usable authorization code. The ceremony is
  /// left in flight; this is noise, not an answer.
  malformed,

  /// Accepted and handed to the ceremony. The ceremony is consumed, so a replay
  /// of the same URL lands on [noActiveCeremony].
  accepted,
}

/// The result of offering one inbound link to the coordinator.
class OidcCallbackResult {
  const OidcCallbackResult(this.outcome, {this.code, this.state, this.error});

  final OidcCallbackOutcome outcome;

  /// The authorization code, present only when [outcome] is
  /// [OidcCallbackOutcome.accepted].
  final String? code;

  /// The echoed `state`, present when it was read and matched.
  final String? state;

  /// The provider's error code, present only on
  /// [OidcCallbackOutcome.providerError].
  final String? error;

  bool get isAccepted => outcome == OidcCallbackOutcome.accepted;

  @override
  String toString() => 'OidcCallbackResult(${outcome.name})';
}

/// One in-flight authorization ceremony.
///
/// The caller that starts a ceremony holds this and awaits [completion]; the
/// coordinator is the only thing that completes it. Wave 2 owns what goes into
/// [codeVerifier] and what happens with the code afterwards — this type is the
/// seam between the two, so the redirect boundary can be built and tested
/// without reaching into the auth controller.
class OidcCeremony {
  OidcCeremony({
    required this.state,
    required this.codeVerifier,
    required this.startedAt,
  });

  /// The opaque anti-forgery value handed to the provider.
  final String state;

  /// The PKCE code verifier for this ceremony. Never leaves the device.
  final String codeVerifier;

  final DateTime startedAt;

  final Completer<OidcCallbackResult> _completer =
      Completer<OidcCallbackResult>();

  /// Resolves once the ceremony ends, however it ends.
  Future<OidcCallbackResult> get completion => _completer.future;

  void _finish(OidcCallbackResult result) {
    if (!_completer.isCompleted) _completer.complete(result);
  }
}

/// THE entry point for anything arriving on the verified redirect boundary.
///
/// Everything that can deliver an OIDC authorization response — an Android App
/// Link, an iOS Universal Link, the router's platform route information — funnels
/// through [submit] or [submitLocation]. There is no second door, and in
/// particular there is no custom-scheme fallback.
class OidcCallbackCoordinator {
  OidcCallbackCoordinator({
    OidcRedirectConfig? config,
    this.ttl = const Duration(minutes: 10),
    DateTime Function()? clock,
  }) : config = config ?? OidcRedirectConfig.fromBuild,
       _now = clock ?? DateTime.now;

  final OidcRedirectConfig config;

  /// How long a started ceremony stays answerable. A callback arriving after it
  /// is treated exactly like one arriving with no ceremony at all: discarded.
  final Duration ttl;
  final DateTime Function() _now;

  OidcCeremony? _pending;

  /// The `redirect_uri` to register with the provider and send on the
  /// authorization request.
  Uri get redirectUri => config.redirectUri;

  /// True while a ceremony is in flight and unexpired.
  bool get hasCeremonyInFlight => _live() != null;

  /// Starts a ceremony. Any previous one is abandoned first: only one
  /// authorization response can ever be the answer to the current sign-in.
  OidcCeremony begin({required String state, required String codeVerifier}) {
    if (state.isEmpty) {
      throw ArgumentError.value(state, 'state', 'must not be empty');
    }
    abandon();
    final ceremony = OidcCeremony(
      state: state,
      codeVerifier: codeVerifier,
      startedAt: _now(),
    );
    _pending = ceremony;
    return ceremony;
  }

  /// Ends any in-flight ceremony without an answer (user cancelled, app
  /// backgrounded past the TTL, sign-out).
  void abandon() {
    final pending = _pending;
    _pending = null;
    pending?._finish(
      const OidcCallbackResult(OidcCallbackOutcome.noActiveCeremony),
    );
  }

  /// Offers a FULL absolute URL — the shape an App Link / Universal Link
  /// delivers.
  OidcCallbackResult submit(Uri incoming) {
    // 1. Is this our boundary at all? Nothing is read from a link that is not.
    if (!config.matches(incoming)) {
      return const OidcCallbackResult(OidcCallbackOutcome.notOurRedirect);
    }

    // 2. Did we ask for this? A callback with no ceremony in flight is
    //    discarded here, BEFORE its query parameters are looked at.
    final ceremony = _live();
    if (ceremony == null) {
      _pending = null;
      return const OidcCallbackResult(OidcCallbackOutcome.noActiveCeremony);
    }

    final query = incoming.queryParameters;

    // 3. Is it the answer to OUR question?
    final returnedState = query['state'];
    if (returnedState == null || returnedState != ceremony.state) {
      return const OidcCallbackResult(OidcCallbackOutcome.stateMismatch);
    }

    // 4. A provider error ends the ceremony.
    final error = query['error'];
    if (error != null && error.isNotEmpty) {
      _pending = null;
      final result = OidcCallbackResult(
        OidcCallbackOutcome.providerError,
        state: returnedState,
        error: error,
      );
      ceremony._finish(result);
      return result;
    }

    // 5. Matching but useless: leave the ceremony in flight.
    final code = query['code'];
    if (code == null || code.isEmpty) {
      return OidcCallbackResult(
        OidcCallbackOutcome.malformed,
        state: returnedState,
      );
    }

    // 6. Accepted, and consumed: a replayed link finds no ceremony.
    _pending = null;
    final result = OidcCallbackResult(
      OidcCallbackOutcome.accepted,
      code: code,
      state: returnedState,
    );
    ceremony._finish(result);
    return result;
  }

  /// Offers a platform ROUTE (path + query only). The host was already enforced
  /// by the OS — the verified intent filter on Android, the associated domain on
  /// iOS — and [OidcRedirectConfig.anchor] refuses any route that tries to carry
  /// a host of its own.
  OidcCallbackResult submitLocation(Uri location) {
    final anchored = config.anchor(location);
    if (anchored == null) {
      return const OidcCallbackResult(OidcCallbackOutcome.notOurRedirect);
    }
    return submit(anchored);
  }

  /// True when [location] is the callback path and therefore belongs to this
  /// coordinator rather than to a screen.
  bool ownsLocation(Uri location) => config.anchor(location) != null;

  OidcCeremony? _live() {
    final pending = _pending;
    if (pending == null) return null;
    if (_now().difference(pending.startedAt) > ttl) {
      _pending = null;
      pending._finish(
        const OidcCallbackResult(OidcCallbackOutcome.noActiveCeremony),
      );
      return null;
    }
    return pending;
  }
}

/// App-wide coordinator. Deliberately its own provider rather than state on the
/// auth controller: the redirect boundary has to exist and be testable before,
/// during and after any particular sign-in attempt.
final oidcCallbackCoordinatorProvider = Provider<OidcCallbackCoordinator>((
  ref,
) {
  final coordinator = OidcCallbackCoordinator();
  ref.onDispose(coordinator.abandon);
  return coordinator;
});
