import 'dart:async';
import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:sentry/sentry.dart';

import '../config/env.dart';
import '../core/api_client.dart';
import '../core/api_exception.dart';
import '../core/biometric_service.dart';
import '../core/cache_crypto.dart';
import '../core/data_scope.dart';
import '../core/messenger.dart';
import '../core/observability.dart';
import '../core/push_service.dart';
import '../core/response_cache.dart';
import '../core/session_fence.dart';
import '../core/session_wipe.dart';
import '../core/token_storage.dart';
import '../models/auth.dart';
import '../repositories/auth_repository.dart';
import '../repositories/push_repository.dart';
import 'impersonation.dart';

enum AuthStatus { unknown, authenticated, unauthenticated }

class AuthState {
  const AuthState({required this.status, this.me, this.locked = false});

  final AuthStatus status;
  final Me? me;

  /// True when the session is valid but held behind the biometric app-lock.
  /// The router keeps such a session on `/lock` until [AuthController.unlock].
  final bool locked;

  bool get isAuthenticated => status == AuthStatus.authenticated;
  bool get isKnown => status != AuthStatus.unknown;

  AuthState copyWith({AuthStatus? status, Me? me, bool? locked}) => AuthState(
        status: status ?? this.status,
        me: me ?? this.me,
        locked: locked ?? this.locked,
      );

  static const unknown = AuthState(status: AuthStatus.unknown);
  static const signedOut = AuthState(status: AuthStatus.unauthenticated);
}

// --- Infrastructure providers ---------------------------------------------

final tokenStorageProvider = Provider<TokenStorage>((ref) => TokenStorage());

/// The live session generation. One instance for the whole app: the API client
/// stamps outgoing work with it and the wipe closes it, which is what makes a
/// sign-out that races an in-flight refresh safe. See [SessionFence].
final sessionFenceProvider = Provider<SessionFence>((ref) => SessionFence());

/// Holder of the per-install key that seals the on-disk response cache.
final cacheCipherProvider = Provider<CacheCipher>((ref) => CacheCipher());

/// The one place a session is torn down. Participants are registered here in
/// the order they must run: in-memory state first (so the UI leaves the
/// authenticated shell immediately), then the durable stores. Every teardown
/// path — explicit sign-out, session expiry, authoritative revocation — calls
/// [SessionWipe.wipe]; no caller clears a subset directly.
final sessionWipeProvider = Provider<SessionWipe>((ref) {
  final wipe = SessionWipe(ref.watch(sessionFenceProvider))
    ..register('session-state', (reason) async {
      ref.read(authControllerProvider.notifier).onSessionWiped(reason);
    })
    ..register('credentials', (_) => ref.read(tokenStorageProvider).clear())
    ..register(
        'response-cache', (_) => ref.read(responseCacheProvider).clear());
  return wipe;
});

final biometricServiceProvider =
    Provider<BiometricService>((ref) => BiometricService());

/// Single FCM client instance (initialised lazily on first authenticated load).
final pushServiceProvider = Provider<PushService>((ref) => PushService());

final pushRepositoryProvider = Provider<PushRepository>((ref) {
  return PushRepository(ref.watch(apiClientProvider).dio);
});

/// On-disk stale-while-revalidate cache for GET responses. Single instance: it
/// carries the current data scope, and it is a registered wipe participant.
final responseCacheProvider = Provider<ResponseCache>(
    (ref) => ResponseCache(cipher: ref.watch(cacheCipherProvider)));

final apiClientProvider = Provider<ApiClient>((ref) {
  final client = ApiClient(
    storage: ref.watch(tokenStorageProvider),
    fence: ref.watch(sessionFenceProvider),
    cache: ref.watch(responseCacheProvider),
    onSessionExpired: () {
      // The server AUTHORITATIVELY ended this session (it refused the refresh,
      // or rejected a freshly-issued token). A transport failure never reaches
      // here — see _RefreshOutcome.unavailable — so a signal blip or a 5xx
      // cannot sign anyone out.
      unawaited(ref.read(authControllerProvider.notifier).onSessionExpired());
    },
    onImpersonationExpired: () {
      // A "view as" request 401'd: the short-lived grant lapsed. Clear it,
      // route back to the reseller area, and tell the user.
      ref.read(impersonationProvider.notifier).expire();
    },
    onCacheState: (fromCache) {
      // Drive the offline banner: a stale-cache fallback flags offline; a fresh
      // network response clears it.
      ref.read(offlineProvider.notifier).set(fromCache);
    },
  );
  return client;
});

final authRepositoryProvider = Provider<AuthRepository>((ref) {
  return AuthRepository(
    dio: ref.watch(apiClientProvider).dio,
    storage: ref.watch(tokenStorageProvider),
  );
});

// --- Auth state controller -------------------------------------------------

final authControllerProvider =
    StateNotifierProvider<AuthController, AuthState>((ref) {
  return AuthController(ref)..bootstrap();
});

/// Convenience: the signed-in user, or null.
final currentUserProvider = Provider<Me?>((ref) {
  return ref.watch(authControllerProvider).me;
});

class AuthController extends StateNotifier<AuthState> {
  AuthController(this._ref) : super(AuthState.unknown);

  final Ref _ref;

  AuthRepository get _repo => _ref.read(authRepositoryProvider);
  TokenStorage get _storage => _ref.read(tokenStorageProvider);
  SessionFence get _fence => _ref.read(sessionFenceProvider);
  SessionWipe get _wipe => _ref.read(sessionWipeProvider);
  ResponseCache get _cache => _ref.read(responseCacheProvider);
  BiometricService get _biometric => _ref.read(biometricServiceProvider);
  PushService get _push => _ref.read(pushServiceProvider);
  PushRepository get _pushRepo => _ref.read(pushRepositoryProvider);

  /// True while a biometric prompt is on screen. Resume-lock checks this so the
  /// prompt's own lifecycle transitions can't re-arm the lock under itself.
  bool _promptActive = false;

  /// Exposed so the lifecycle observer can tell a pause caused by our own
  /// biometric prompt (some Android OEMs host it in a separate activity, which
  /// pauses us) from a real backgrounding, and skip re-arming the lock for the
  /// former. iOS prompts only emit `inactive`, so this never fires there.
  bool get promptActive => _promptActive;

  /// In-memory mirror of the persisted biometric opt-in so [lockOnResume] can
  /// lock synchronously on resume instead of leaving the previous screen
  /// visible (and tappable) while secure storage is read.
  bool _biometricArmed = false;

  /// Where the router should return the user once the lock is satisfied.
  /// Stashed by the redirect when it diverts to /lock, consumed exactly once.
  String? _lockReturnLocation;

  /// Restore a previous session on cold start.
  ///
  /// Optimistic: if we hold a cached profile we render straight to the
  /// authenticated app and refresh `/auth/me` behind it, so the splash isn't
  /// blocked on a (potentially slow) network round-trip. We only force the user
  /// back to login when the server actively rejects the session (401/403) — a
  /// transient network failure keeps the cached session rather than logging a
  /// connected user out because their signal dropped.
  Future<void> bootstrap() async {
    final bundle = await _storage.readBundle();
    if (bundle == null) {
      state = AuthState.signedOut;
      return;
    }
    // Resume the persisted session under its own generation (it is monotonic
    // across restarts, so work left over from before a crash still fences out)
    // and point the cache at the identity the record says these tokens belong
    // to — before a single request goes out.
    final generation = bundle.generation;
    _fence.open(generation);
    _cache.useScope(bundle.scope);

    final locked = await _shouldLockOnLaunch();
    final cached = await _readCachedProfile();
    if (cached != null) {
      state = AuthState(
        status: AuthStatus.authenticated,
        me: cached,
        locked: locked,
      );
    }

    try {
      final me = await _repo.me();
      if (!_fence.holds(generation)) {
        // The session ended while /auth/me was in flight — the user signed out
        // from the lock screen, or a 401 elsewhere revoked it. Don't resurrect
        // it from a stale response, and don't write its profile back.
        return;
      }
      await _adoptIdentity(me, generation);
      state = AuthState(
        status: AuthStatus.authenticated,
        me: me,
        // Preserve the *current* lock state, not the launch decision: the user
        // may have unlocked while /auth/me was in flight, and re-applying the
        // stale `locked` capture would re-lock them. Without a cached profile
        // no lock screen was shown yet, so the launch decision still stands.
        locked: cached != null ? state.locked : locked,
      );
    } on ApiException catch (e) {
      if (e.statusCode == 401 || e.statusCode == 403) {
        // The server actively rejected the session. That is authoritative:
        // tear the whole thing down through the one coordinator.
        await _wipe.wipe(SessionWipeReason.credentialsRevoked);
        return;
      }
      // Anything else is "we could not ask" — a timeout, no signal, a 5xx from
      // an overloaded gateway. NEVER discard credentials for that. With a
      // cached profile we keep rendering optimistically; without one there is
      // nothing to render, so the user lands on /login — but their session is
      // still on the device and the next successful bootstrap restores it.
      if (cached == null) state = AuthState.signedOut;
    } catch (_) {
      if (cached == null) state = AuthState.signedOut;
    }
  }

  /// Stamp the resolved identity onto the session: onto the credential record
  /// (so a cold start knows whose tokens these are before `/auth/me` answers)
  /// and onto the response cache (so this account's bodies land in their own
  /// partition). Fenced — a late `/auth/me` from a dead session writes nothing.
  Future<void> _adoptIdentity(Me me, int generation) async {
    final scope = MobileDataScope(tenant: Env.apiBaseUrl, principal: me.id);
    await _storage.adoptScope(generation: generation, scope: scope);
    _cache.useScope(scope);
    await _persistProfile(me, generation: generation);
  }

  Future<Me?> _readCachedProfile() async {
    final raw = await _storage.readProfile();
    if (raw == null) return null;
    try {
      return Me.fromJson(jsonDecode(raw) as Map<String, dynamic>);
    } catch (_) {
      return null;
    }
  }

  Future<void> _persistProfile(Me me, {int? generation}) async {
    try {
      await _storage.saveProfile(jsonEncode(me.toJson()),
          generation: generation);
    } catch (_) {
      // Caching the profile is best-effort; never fail bootstrap over it.
    }
  }

  /// Lock on launch only when the user opted in AND biometrics are still
  /// usable. If biometrics were removed since opt-in we proceed unlocked rather
  /// than stranding the user.
  Future<bool> _shouldLockOnLaunch() async {
    // Cache the opt-in so lockOnResume can act without an async storage read.
    _biometricArmed = await _storage.isBiometricEnabled();
    if (!_biometricArmed) return false;
    return _biometric.isAvailable();
  }

  // --- Biometric app-lock --------------------------------------------------

  /// Prompt to unlock a locked session. On success the router redirects off
  /// `/lock`. Does not store or replay any credential — it only reveals the
  /// session tokens already held in secure storage.
  Future<bool> unlock() async {
    _promptActive = true;
    try {
      final ok = await _biometric.authenticate(reason: 'Unlock ${Brand.name}');
      if (ok) state = state.copyWith(locked: false);
      return ok;
    } finally {
      _promptActive = false;
    }
  }

  /// Re-arm the lock when the app returns from the background. No-op unless the
  /// session is authenticated, currently unlocked, opted-in, and biometrics are
  /// available — and never while a prompt is already showing.
  Future<void> lockOnResume() async {
    if (_promptActive || state.locked || !state.isAuthenticated) return;
    if (!_biometricArmed) return;
    // Lock synchronously (the opt-in is cached in memory) so the previous
    // screen isn't left visible and tappable while the availability check
    // round-trips the platform channel.
    state = state.copyWith(locked: true);
    // If biometrics were removed while backgrounded, roll back rather than
    // stranding the user behind a lock they can't satisfy.
    if (!await _biometric.isAvailable()) {
      state = state.copyWith(locked: false);
    }
  }

  /// Remember where the lock interrupted the user so [takeLockReturnLocation]
  /// can send them back after a successful unlock. Launch/auth routes aren't
  /// worth returning to — those fall through to the portal home instead.
  void stashLockReturnLocation(String location) {
    const skip = {
      '/splash',
      '/login',
      '/lock',
      '/mfa',
      '/forgot-password',
      '/reset-password',
    };
    if (skip.contains(location)) return;
    _lockReturnLocation = location;
  }

  /// One-shot read of the stashed return location — cleared on read (and on
  /// sign-out) so a later logout/login can't bounce to a stale screen.
  String? takeLockReturnLocation() {
    final location = _lockReturnLocation;
    _lockReturnLocation = null;
    return location;
  }

  Future<bool> biometricAvailable() => _biometric.isAvailable();

  Future<bool> isBiometricLockEnabled() => _storage.isBiometricEnabled();

  /// One-time post-login enrollment prompt bookkeeping (see
  /// BiometricEnrollmentPrompt): offer "Sign in with Face ID/fingerprint" at
  /// most once per device.
  Future<bool> biometricPromptSeen() => _storage.biometricPromptSeen();

  Future<void> markBiometricPromptSeen() => _storage.setBiometricPromptSeen();

  /// Turn the lock on. Requires a successful biometric check first so the user
  /// proves they can satisfy the lock before we enable it. Returns false if
  /// unavailable or the check was cancelled/failed.
  Future<bool> enableBiometricLock() async {
    if (!await _biometric.isAvailable()) return false;
    _promptActive = true;
    try {
      final ok = await _biometric.authenticate(
          reason: 'Confirm to enable biometric unlock');
      if (ok) {
        _biometricArmed = true;
        await _storage.setBiometricEnabled(true);
      }
      return ok;
    } finally {
      _promptActive = false;
    }
  }

  Future<void> disableBiometricLock() {
    _biometricArmed = false;
    return _storage.setBiometricEnabled(false);
  }

  /// Returns the [LoginResult] so the UI can branch into the MFA flow.
  /// Breadcrumbs the attempt/outcome (provider only — never the credentials).
  Future<LoginResult> login({
    required String username,
    required String password,
    String? provider,
  }) async {
    Log.breadcrumb('login attempt',
        category: 'auth', data: {'provider': provider ?? 'local'});
    try {
      final result = await _repo.login(
        username: username,
        password: password,
        provider: provider,
      );
      if (result.isAuthenticated) {
        await _loadMe();
        Log.breadcrumb('login success', category: 'auth');
      } else if (result.mfaRequired) {
        Log.breadcrumb('login -> mfa required', category: 'auth');
      }
      return result;
    } catch (e) {
      // Summarised, never interpolated: a DioException stringifies the request
      // URL and the whole response body, and this is the login path.
      Log.breadcrumb('login failed',
          category: 'auth',
          level: SentryLevel.warning,
          data: {'error': Log.describeError(e)});
      rethrow;
    }
  }

  Future<void> verifyMfa(
      {required String mfaToken, required String code}) async {
    await _repo.verifyMfa(mfaToken: mfaToken, code: code);
    await _loadMe();
    Log.breadcrumb('mfa verified', category: 'auth');
  }

  Future<void> _loadMe() async {
    // The repository has just written a fresh credential record (login / MFA
    // verify). Adopt its generation before any request goes out, so everything
    // from here on is fenced to THIS session.
    final generation = (await _storage.readBundle())?.generation;
    if (generation != null) _fence.open(generation);

    final me = await _repo.me();
    if (generation != null) {
      if (!_fence.holds(generation)) return;
      await _adoptIdentity(me, generation);
    }
    // Re-sync the cached opt-in: it survives a session-expiry token wipe, so a
    // fresh password login must re-arm the resume lock without a cold start.
    _biometricArmed = await _storage.isBiometricEnabled();
    state = AuthState(status: AuthStatus.authenticated, me: me);
    await Sentry.configureScope(
        (scope) => scope.setUser(SentryUser(id: me.id)));
    // Register this device for push, best-effort and non-blocking. No-op when
    // FCM isn't configured for the build.
    unawaited(_syncPushRegistration());
  }

  /// Initialise FCM, request permission, and register the device token with
  /// the backend. Entirely best-effort — a missing platform config or a denied
  /// permission just leaves push disabled; never affects the auth flow.
  Future<void> _syncPushRegistration() async {
    try {
      if (!await _push.init()) return;
      await _push.requestPermission();
      final token = await _push.currentToken();
      if (token == null || token.isEmpty) return;
      final platform = PushService.platformTag();
      await _pushRepo.registerToken(token: token, platform: platform);
      _push.wireForegroundHandlers(
        (t) => _pushRepo.registerToken(token: t, platform: platform),
      );
      Log.breadcrumb('push: device registered', category: 'push');
    } catch (e) {
      Log.breadcrumb('push: registration skipped',
          category: 'push',
          level: SentryLevel.warning,
          data: {'error': Log.describeError(e)});
    }
  }

  /// De-register the device token while the session is still valid (the
  /// DELETE needs auth), then drop the local FCM token.
  Future<void> _unregisterPush() async {
    try {
      final token = await _push.currentToken();
      if (token != null && token.isNotEmpty) {
        await _pushRepo.unregisterToken(token);
      }
      await _push.deleteToken();
    } catch (e) {
      Log.breadcrumb('push: unregister skipped',
          category: 'push', data: {'error': Log.describeError(e)});
    }
  }

  void setMe(Me me) {
    state = state.copyWith(status: AuthStatus.authenticated, me: me);
  }

  /// Re-fetch the profile (e.g. after an avatar or profile-field change).
  Future<void> reloadProfile() => _loadMe();

  Future<void> logout() async {
    Log.breadcrumb('logout', category: 'auth');
    // De-register the device for push, and revoke the server-side session,
    // while the credentials are still valid — both calls need auth.
    await _unregisterPush();
    await _repo.logout();
    // Explicit sign-out is a full reset: drop the biometric opt-in so the next
    // user starts from a clean password login. A DEVICE preference, not
    // session state — which is why it lives here and not in the wipe (a
    // session expiry deliberately leaves the opt-in armed).
    await _storage.setBiometricEnabled(false);
    // Everything else — credentials, cached profile, response cache, in-memory
    // session — goes through the one coordinator.
    await _wipe.wipe(SessionWipeReason.userSignedOut);
  }

  /// Soft-delete (cancel) the account, then sign out. The server cancels the
  /// subscriber (blocking future login); [logout] then clears the local session
  /// so the user lands back on the sign-in screen.
  Future<void> deleteAccount({String? reason}) async {
    Log.breadcrumb('delete_account', category: 'auth');
    await _repo.requestAccountDeletion(reason: reason);
    await logout();
  }

  /// The server ended this session out from under us — the refresh was refused,
  /// or a freshly-issued token was rejected on first use. Authoritative, so the
  /// whole session goes.
  Future<void> onSessionExpired() =>
      _wipe.wipe(SessionWipeReason.sessionExpired);

  /// Registered with [SessionWipe] as the `session-state` participant, and the
  /// FIRST one to run: this is the synchronous part of a teardown, so the
  /// router has already left the authenticated shell before the (asynchronous,
  /// best-effort) disk clearing starts. Never call it directly — a session is
  /// torn down through the coordinator or not at all.
  void onSessionWiped(SessionWipeReason reason) {
    Log.breadcrumb('session wiped (${reason.name})', category: 'auth');
    _biometricArmed = false;
    _lockReturnLocation = null;
    // Repoint the cache away from the signed-out identity, so nothing that is
    // still unwinding can read or write that account's partition.
    _cache.useScope(MobileDataScope.anonymous);
    state = AuthState.signedOut;
    unawaited(Sentry.configureScope((scope) => scope.setUser(null)));
  }
}
