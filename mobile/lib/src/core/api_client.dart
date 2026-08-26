import 'dart:io' show Platform;

import 'package:dio/dio.dart';
import 'package:sentry/sentry.dart' show SentryLevel;

import '../config/env.dart';
import 'observability.dart';
import 'response_cache.dart';
import 'session_fence.dart';
import 'token_storage.dart';

/// What came back from an attempt to refresh the access token.
///
/// The distinction between [rejected] and [unavailable] is the whole point:
/// before it existed, *any* failure of `/auth/refresh` — including a dropped
/// connection or a 502 from an overloaded gateway — signed the user out. A
/// server that cannot answer is not a server that says no.
enum _RefreshOutcome {
  /// The server issued new credentials and they were persisted.
  renewed,

  /// The server answered authoritatively that this session is over (4xx), or
  /// there was no refresh token to try with. The session is gone.
  rejected,

  /// We could not get an answer: timeout, dropped connection, 5xx. The session
  /// is untouched and the caller must not sign the user out.
  unavailable,

  /// The refresh completed but belonged to a session that has since ended (a
  /// sign-out raced it). The result was discarded and nothing was written.
  stale
}

/// Thin wrapper around Dio configured for the DotMac API.
///
/// Responsibilities:
///  * attach `Authorization: Bearer <access_token>` to every request,
///  * on a 401, transparently refresh the token via `/auth/refresh` and
///    replay the original request once,
///  * notify the app when the session can no longer be recovered.
///
/// Two invariants worth stating explicitly, because they are what the tests in
/// `session_security_test.dart` pin down:
///
///  * **Concurrent 401s cause exactly one refresh.** Every screen fires its own
///    GET on load; when the access token expires they all 401 within a few
///    milliseconds of each other. Without coalescing, each one posts its own
///    `/auth/refresh` with the *same* refresh token — a refresh storm against a
///    backend that rotates refresh tokens, where all but one attempt is
///    guaranteed to fail and take the session down with it.
///  * **A refresh can never outlive its session.** The generation the refresh
///    started under is checked again before anything is written, and the write
///    itself is fenced in [TokenStorage]. A sign-out that races an in-flight
///    refresh cannot be undone by it.
class ApiClient {
  ApiClient(
      {required TokenStorage storage,
      SessionFence? fence,
      this.cache,
      this.onSessionExpired,
      this.onImpersonationExpired,
      this.onCacheState})
      : _storage = storage,
        _fence = fence ?? SessionFence() {
    _dio = Dio(BaseOptions(
        baseUrl: Env.apiRoot,
        connectTimeout: const Duration(seconds: 15),
        receiveTimeout: const Duration(seconds: 20),
        contentType: Headers.jsonContentType,
        // Native client: the backend can't hand us an httpOnly refresh cookie,
        // so opt into receiving the refresh token in the JSON body (we persist
        // it in the platform secure store). See app/services/auth_flow.py.
        // A descriptive User-Agent gives each login a meaningful row on the
        // "Active sessions" screen (backend stores it; the app derives the
        // device label from it) instead of the generic Dart default.
        headers: {
          'X-Auth-Refresh-In-Body': 'true',
          'User-Agent':
              'DotmacSelfcare/${Brand.version} (${Platform.operatingSystem})'
        },
        // We parse error bodies ourselves; let any status through to the
        // interceptor/caller rather than throwing on every 4xx blindly.
        validateStatus: (status) => status != null && status < 500));

    _dio.interceptors.add(
        InterceptorsWrapper(onRequest: _onRequest, onResponse: _onResponse));

    // Stale-while-revalidate fallback: serve the last good GET body when a
    // request fails at the transport level (timeout/reset/5xx). Added after the
    // auth interceptor so a post-refresh replay that times out — now rejected as
    // a DioException — also gets served from cache. No-op when no cache wired.
    if (cache != null) {
      _dio.interceptors.add(
          CacheInterceptor(cache!, onCacheState: onCacheState, fence: _fence));
    }

    // Breadcrumb every call (method + path + status only — never headers/body,
    // which carry the bearer token and passwords) so crashes have an API trail.
    // Log.breadcrumb redacts on top of that: a path can still arrive with an
    // inline query string, and a DioException stringifies its whole response.
    _dio.interceptors.add(InterceptorsWrapper(onRequest: (options, handler) {
      Log.breadcrumb('${options.method} ${options.path}', category: 'http');
      handler.next(options);
    }, onResponse: (response, handler) {
      Log.breadcrumb('${response.statusCode} ${response.requestOptions.path}',
          category: 'http',
          level: (response.statusCode ?? 0) >= 400
              ? SentryLevel.warning
              : SentryLevel.info);
      handler.next(response);
    }, onError: (err, handler) {
      Log.breadcrumb('${err.type.name} ${err.requestOptions.path}',
          category: 'http', level: SentryLevel.error);
      handler.next(err);
    }));
  }

  final TokenStorage _storage;
  final SessionFence _fence;

  /// Optional on-disk response cache for stale-while-revalidate fallback.
  final ResponseCache? cache;

  /// Invoked when the session was *authoritatively* ended — the server said no,
  /// not "the server could not be reached". Wired to the session wipe.
  final void Function()? onSessionExpired;

  /// Invoked when a request made under reseller "view as" gets a 401 — the
  /// short-lived impersonation grant lapsed. The handler clears impersonation
  /// and surfaces it to the user (no silent failure).
  final void Function()? onImpersonationExpired;

  /// Notified when a GET is served from the stale on-disk cache (true) or
  /// completes fresh from the network (false). Drives the offline banner.
  final void Function(bool fromCache)? onCacheState;

  late final Dio _dio;
  Dio get dio => _dio;

  /// When set, every request authenticates as the impersonated customer
  /// (reseller "view as" — short-lived, server-enforced read-only). The
  /// reseller's own tokens stay in storage untouched; clearing this restores
  /// them instantly. 401s under impersonation mean the 15-minute session
  /// lapsed — they must NOT trigger a refresh of the reseller's token into
  /// customer requests.
  String? impersonationToken;

  // Single-flight guard so concurrent 401s share one refresh round-trip.
  Future<_RefreshOutcome>? _refreshing;

  // Cached after first read; stable for the app's lifetime.
  String? _cachedDeviceId;
  Future<String> _deviceId() async =>
      _cachedDeviceId ??= await _storage.deviceId();

  Future<void> _onRequest(
      RequestOptions options, RequestInterceptorHandler handler) async {
    if (options.extra['skipAuth'] != true) {
      final override = impersonationToken;
      if (override != null) {
        options.headers['Authorization'] = 'Bearer $override';
        options.extra['impersonated'] = true;
      } else {
        final token = await _storage.readAccessToken();
        if (token != null) {
          options.headers['Authorization'] = 'Bearer $token';
        }
      }
    }
    // Stamp the session this request belongs to. Anything that comes back
    // carrying a generation (or a data scope) that is no longer current is a
    // straggler from a session that has ended and must not write anything —
    // see CacheInterceptor._stillCurrent and TokenStorage.renewSession.
    options.extra['sessionGeneration'] = _fence.current;
    final scope = cache?.scope;
    if (scope != null) options.extra['scopeSegment'] = scope.segment;
    // Stable per-install id so the backend keeps one session per device
    // (login/refresh replace this device's prior session). Sent on all calls.
    options.headers['X-Device-Id'] = await _deviceId();
    handler.next(options);
  }

  Future<void> _onResponse(
      Response response, ResponseInterceptorHandler handler) async {
    final isAuthRetry = response.requestOptions.extra['authRetried'] == true;
    final skipAuth = response.requestOptions.extra['skipAuth'] == true;

    final wasImpersonated =
        response.requestOptions.extra['impersonated'] == true;

    // A 401 while impersonating means the short-lived "view as" grant lapsed.
    // Never refresh the reseller's token into a customer request — instead
    // clear impersonation and surface it, rather than failing silently.
    if (response.statusCode == 401 && !skipAuth && wasImpersonated) {
      onImpersonationExpired?.call();
      handler.next(response);
      return;
    }

    if (response.statusCode == 401 &&
        !isAuthRetry &&
        !skipAuth &&
        !wasImpersonated) {
      final outcome = await _refreshToken();
      switch (outcome) {
        case _RefreshOutcome.renewed:
          try {
            final replay = await _replay(response.requestOptions);
            // A brand-new access token rejected on its first use is the server
            // telling us this session is over (revoked elsewhere, account
            // disabled). That IS authoritative — expire it.
            if (replay.statusCode == 401) onSessionExpired?.call();
            return handler.resolve(replay);
          } on DioException catch (e) {
            // Refresh succeeded but the replay itself failed — typically a
            // timeout or connection reset under server load, not an auth
            // problem. Surface THAT error so the UI shows a retryable network
            // state; falling through would deliver the stale original 401 and
            // mislabel the failure as "(401)".
            return handler.reject(e);
          }
        case _RefreshOutcome.rejected:
          onSessionExpired?.call();
        case _RefreshOutcome.unavailable:
          // The network, not the server, said no. Deliver the 401 to the
          // caller and leave the session exactly as it was — a signal blip or
          // a gateway hiccup must never sign anyone out.
          Log.breadcrumb('refresh unavailable; session kept', category: 'auth');
        case _RefreshOutcome.stale:
          // The session ended while the refresh was in flight. Nothing to do:
          // the wipe that ended it has already run.
          break;
      }
    }
    handler.next(response);
  }

  Future<Response> _replay(RequestOptions options) {
    options.extra['authRetried'] = true;
    return _dio.fetch(options);
  }

  /// Coalesce concurrent refreshes onto one round-trip. Every waiter resolves
  /// on the same outcome, so N simultaneous 401s produce exactly one
  /// `/auth/refresh` and N replays.
  Future<_RefreshOutcome> _refreshToken() {
    return _refreshing ??= _doRefresh().whenComplete(() => _refreshing = null);
  }

  Future<_RefreshOutcome> _doRefresh() async {
    final bundle = await _storage.readBundle();
    final refresh = bundle?.refreshToken;
    if (bundle == null || refresh == null) {
      // No credential record, or one with nothing to recover with. There is no
      // network answer that could change this, so it is authoritative.
      return _RefreshOutcome.rejected;
    }
    final generation = bundle.generation;
    // Fast path: the in-memory fence already knows this session is over.
    if (_fence.isOpen && !_fence.holds(generation)) {
      return _RefreshOutcome.stale;
    }
    // Whether the fence was actually guarding this refresh when it started. If
    // it was, a fence that is CLOSED when we come back means a sign-out ran
    // while we were waiting — which a plain `isOpen` check would miss.
    final fenced = _fence.isOpen;

    try {
      final res = await _dio.post('/auth/refresh',
          data: {'refresh_token': refresh},
          options: Options(extra: {'skipAuth': true}));
      final status = res.statusCode ?? 0;
      if (status == 200 && res.data is Map) {
        final data = res.data as Map;
        final access = data['access_token'] as String?;
        if (access != null && access.isNotEmpty) {
          // Re-check after the round trip: this is where a sign-out that ran
          // while we were waiting gets caught.
          if (fenced && !_fence.holds(generation)) {
            Log.breadcrumb('refresh result discarded: session ended in flight',
                category: 'auth');
            return _RefreshOutcome.stale;
          }
          // The durable check. Even with no fence (a rebuilt client), storage
          // refuses a write whose generation is not the stored one — and after
          // a wipe there is no stored record at all.
          final persisted = await _storage.renewSession(
              generation: generation,
              accessToken: access,
              refreshToken: data['refresh_token'] as String?);
          return persisted ? _RefreshOutcome.renewed : _RefreshOutcome.stale;
        }
      }
      // A 4xx from /auth/refresh is the server refusing this session.
      if (status >= 400 && status < 500) return _RefreshOutcome.rejected;
      // Anything else (a 2xx we cannot parse) is not a refusal either.
      return _RefreshOutcome.unavailable;
    } on DioException catch (e) {
      return _isTransportFailure(e)
          ? _RefreshOutcome.unavailable
          : _RefreshOutcome.rejected;
    }
  }

  /// Could-not-get-an-answer, as opposed to an answer of "no". `validateStatus`
  /// lets every 4xx through as a response, so a thrown `badResponse` is a 5xx.
  bool _isTransportFailure(DioException e) {
    switch (e.type) {
      case DioExceptionType.connectionTimeout:
      case DioExceptionType.sendTimeout:
      case DioExceptionType.receiveTimeout:
      case DioExceptionType.connectionError:
      case DioExceptionType.cancel:
      case DioExceptionType.unknown:
        return true;
      case DioExceptionType.badCertificate:
        // Never an auth answer — and never a reason to hand the session away.
        return true;
      case DioExceptionType.badResponse:
        return (e.response?.statusCode ?? 500) >= 500;
    }
  }
}
