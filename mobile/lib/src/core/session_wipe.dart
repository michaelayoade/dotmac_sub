import 'session_fence.dart';

/// Why a session is being torn down. Recorded on the wipe breadcrumb; the
/// participants themselves deliberately do *not* branch on it — a wipe clears
/// everything, whatever prompted it.
enum SessionWipeReason {
  /// The user tapped sign out (or deleted their account).
  userSignedOut,

  /// The refresh path could not recover the session (no refresh token, or the
  /// server answered the refresh authoritatively).
  sessionExpired,

  /// The server rejected a live request with an authoritative 401/403 — the
  /// session was revoked out from under us (another device signed us out, an
  /// admin revoked the session, the account was disabled).
  credentialsRevoked
}

/// One clearing step: credentials, the cached profile, the response cache, the
/// in-memory session state.
typedef SessionWipeStep = Future<void> Function(SessionWipeReason reason);

/// The single place a session is torn down.
///
/// Before this existed, "sign out" and "session expired" each cleared their own
/// idea of what a session consists of, and they disagreed: explicit logout
/// cleared the response cache and the biometric opt-in but never the persisted
/// tokens (the repository did that, separately); session expiry cleared the
/// response cache but left both the tokens *and* the cached profile on disk.
/// Every new piece of session state was one more thing to remember to add to
/// two-and-a-half call sites, and forgetting one is silent.
///
/// So the participants register themselves once and every teardown path calls
/// [wipe]. Adding a new kind of session state means registering it here, not
/// auditing call sites — and no caller is allowed to clear a subset directly
/// (`session_wipe_contract_test.dart` fails the build if one does).
///
/// Two ordering rules make this safe rather than merely tidy:
///
///  1. The [SessionFence] is closed *first and synchronously*, before any
///     participant runs. From that instant, anything still in flight —
///     a token refresh, a `/auth/me`, a cache write — carries a stale
///     generation and is refused. Clearing storage while writers are still
///     considered live would just race them.
///  2. Participants run in registration order, and the first one runs
///     synchronously within the [wipe] call. The in-memory session state is
///     registered first, so the UI has already left the authenticated shell
///     before the (asynchronous, best-effort) disk clearing begins.
class SessionWipe {
  SessionWipe(this._fence);

  final SessionFence _fence;
  final Map<String, SessionWipeStep> _participants = {};
  Future<void>? _inFlight;

  /// Registered participants, in the order they will run. Exposed so the
  /// contract test can assert the registry is complete.
  Iterable<String> get participants => _participants.keys;

  /// Register (or replace) a clearing step. Replacing is intentional: provider
  /// containers are rebuilt in tests, and a stale closure holding a disposed
  /// container must not survive.
  void register(String name, SessionWipeStep step) {
    _participants[name] = step;
  }

  /// Atomically end the session: fence closed, then every participant cleared.
  ///
  /// Concurrent calls coalesce onto one run — an authoritative 401 arriving
  /// while the user is already tapping "sign out" must not start a second
  /// teardown that races the first.
  Future<void> wipe(SessionWipeReason reason) {
    final running = _inFlight;
    if (running != null) return running;
    // Synchronously, before anything awaits: nothing in flight may write again.
    _fence.close();
    final run = _runAll(reason);
    _inFlight = run;
    return run.whenComplete(() => _inFlight = null);
  }

  Future<void> _runAll(SessionWipeReason reason) async {
    // Snapshot: a participant is allowed to register another one (a rebuilt
    // provider) without mutating the collection we are iterating.
    for (final step in _participants.values.toList(growable: false)) {
      try {
        await step(reason);
      } catch (_) {
        // One participant failing (a locked keystore, a cache directory that
        // no longer exists) must not leave the rest of the session behind.
        // Every step is independently best-effort and idempotent.
      }
    }
  }
}
