import 'package:dio/dio.dart';
import 'package:sentry/sentry.dart' show SentryLevel;

import '../config/env.dart';
import 'observability.dart';
import 'response_cache.dart';
import 'token_storage.dart';

/// Thin wrapper around Dio configured for the DotMac API.
///
/// Responsibilities:
///  * attach `Authorization: Bearer <access_token>` to every request,
///  * on a 401, transparently refresh the token via `/auth/refresh` and
///    replay the original request once,
///  * notify the app when the session can no longer be recovered.
class ApiClient {
  ApiClient({required TokenStorage storage, this.cache, this.onSessionExpired})
      : _storage = storage {
    _dio = Dio(
      BaseOptions(
        baseUrl: Env.apiRoot,
        connectTimeout: const Duration(seconds: 15),
        receiveTimeout: const Duration(seconds: 20),
        contentType: Headers.jsonContentType,
        // Native client: the backend can't hand us an httpOnly refresh cookie,
        // so opt into receiving the refresh token in the JSON body (we persist
        // it in the platform secure store). See app/services/auth_flow.py.
        headers: const {'X-Auth-Refresh-In-Body': 'true'},
        // We parse error bodies ourselves; let any status through to the
        // interceptor/caller rather than throwing on every 4xx blindly.
        validateStatus: (status) => status != null && status < 500,
      ),
    );

    _dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: _onRequest,
        onResponse: _onResponse,
      ),
    );

    // Stale-while-revalidate fallback: serve the last good GET body when a
    // request fails at the transport level (timeout/reset/5xx). Added after the
    // auth interceptor so a post-refresh replay that times out — now rejected as
    // a DioException — also gets served from cache. No-op when no cache wired.
    if (cache != null) {
      _dio.interceptors.add(CacheInterceptor(cache!));
    }

    // Breadcrumb every call (method + path + status only — never headers/body,
    // which carry the bearer token and passwords) so crashes have an API trail.
    _dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          Log.breadcrumb(
            '${options.method} ${options.path}',
            category: 'http',
          );
          handler.next(options);
        },
        onResponse: (response, handler) {
          Log.breadcrumb(
            '${response.statusCode} ${response.requestOptions.path}',
            category: 'http',
            level: (response.statusCode ?? 0) >= 400
                ? SentryLevel.warning
                : SentryLevel.info,
          );
          handler.next(response);
        },
        onError: (err, handler) {
          Log.breadcrumb(
            '${err.type.name} ${err.requestOptions.path}',
            category: 'http',
            level: SentryLevel.error,
          );
          handler.next(err);
        },
      ),
    );
  }

  final TokenStorage _storage;

  /// Optional on-disk response cache for stale-while-revalidate fallback.
  final ResponseCache? cache;

  /// Invoked when refresh fails and the user must re-authenticate.
  final void Function()? onSessionExpired;

  late final Dio _dio;
  Dio get dio => _dio;

  // Single-flight guard so concurrent 401s share one refresh round-trip.
  Future<bool>? _refreshing;

  Future<void> _onRequest(
    RequestOptions options,
    RequestInterceptorHandler handler,
  ) async {
    if (options.extra['skipAuth'] != true) {
      final token = await _storage.readAccessToken();
      if (token != null) {
        options.headers['Authorization'] = 'Bearer $token';
      }
    }
    handler.next(options);
  }

  Future<void> _onResponse(
    Response response,
    ResponseInterceptorHandler handler,
  ) async {
    final isAuthRetry = response.requestOptions.extra['authRetried'] == true;
    final skipAuth = response.requestOptions.extra['skipAuth'] == true;

    if (response.statusCode == 401 && !isAuthRetry && !skipAuth) {
      final refreshed = await _refreshToken();
      if (refreshed) {
        try {
          final replay = await _replay(response.requestOptions);
          return handler.resolve(replay);
        } on DioException catch (e) {
          // Refresh succeeded but the replay itself failed — typically a
          // timeout or connection reset under server load, not an auth
          // problem. Surface THAT error so the UI shows a retryable network
          // state; falling through would deliver the stale original 401 and
          // mislabel the failure as "(401)".
          return handler.reject(e);
        }
      } else {
        onSessionExpired?.call();
      }
    }
    handler.next(response);
  }

  Future<Response> _replay(RequestOptions options) {
    options.extra['authRetried'] = true;
    return _dio.fetch(options);
  }

  Future<bool> _refreshToken() {
    // Coalesce concurrent refreshes.
    return _refreshing ??= _doRefresh().whenComplete(() => _refreshing = null);
  }

  Future<bool> _doRefresh() async {
    final refresh = await _storage.readRefreshToken();
    if (refresh == null) return false;
    try {
      final res = await _dio.post(
        '/auth/refresh',
        data: {'refresh_token': refresh},
        options: Options(extra: {'skipAuth': true}),
      );
      if (res.statusCode == 200 && res.data is Map) {
        final data = res.data as Map;
        final access = data['access_token'] as String?;
        if (access != null) {
          await _storage.save(
            accessToken: access,
            refreshToken: data['refresh_token'] as String? ?? refresh,
          );
          return true;
        }
      }
    } catch (_) {
      // ignore; treated as unrecoverable below
    }
    return false;
  }
}
