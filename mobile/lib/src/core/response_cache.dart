import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:path_provider/path_provider.dart';

import 'observability.dart';

/// On-disk cache of the last successful JSON body for idempotent GETs, used as a
/// *stale fallback* when the network fails (timeout / connection reset / 5xx /
/// offline). It lets a card render its last-known value during a server-overload
/// blip instead of flipping to an error.
///
/// Bodies are plain JSON: tokens live in the secure store and are never part of
/// a response body, so nothing secret is written here. The cache is cleared on
/// logout / session expiry so one account never sees another's data.
class ResponseCache {
  ResponseCache({Directory? directory, Future<Directory> Function()? openDir})
    : _dir = directory,
      _openDir = openDir ?? _defaultDir;

  final Future<Directory> Function() _openDir;
  Directory? _dir;
  Future<Directory>? _opening;

  static Future<Directory> _defaultDir() async {
    final base = await getApplicationSupportDirectory();
    final dir = Directory('${base.path}/api_cache');
    if (!await dir.exists()) {
      await dir.create(recursive: true);
    }
    return dir;
  }

  Future<Directory> _ensureDir() async =>
      _dir ??= await (_opening ??= _openDir());

  /// Filesystem-safe, collision-resistant name for a request signature. The
  /// hash suffix is a deterministic FNV-1a so the same request maps to the same
  /// file across app launches (String.hashCode is not guaranteed stable).
  String _fileName(String key) {
    final safe = key.replaceAll(RegExp(r'[^A-Za-z0-9._-]'), '_');
    final capped = safe.length <= 80 ? safe : safe.substring(0, 80);
    return '${capped}_${_fnv1a(key)}.json';
  }

  static int _fnv1a(String s) {
    var h = 0x811c9dc5;
    for (final c in s.codeUnits) {
      h = (h ^ c) & 0xffffffff;
      h = (h * 0x01000193) & 0xffffffff;
    }
    return h;
  }

  Future<File> _file(String key) async =>
      File('${(await _ensureDir()).path}/${_fileName(key)}');

  Future<void> write(String key, Object? data) async {
    if (data == null) return;
    try {
      await (await _file(key)).writeAsString(jsonEncode(data));
    } catch (e) {
      // A cache write must never break a request.
      Log.breadcrumb('cache write failed: $e', category: 'cache');
    }
  }

  Future<Object?> read(String key) async {
    try {
      final file = await _file(key);
      if (!await file.exists()) return null;
      return jsonDecode(await file.readAsString());
    } catch (e) {
      Log.breadcrumb('cache read failed: $e', category: 'cache');
      return null;
    }
  }

  Future<void> clear() async {
    try {
      final dir = await _ensureDir();
      if (await dir.exists()) await dir.delete(recursive: true);
    } catch (_) {
      // Best-effort; a failed clear is not worth surfacing.
    } finally {
      _dir = null;
      _opening = null;
    }
  }
}

/// Dio interceptor that write-throughs successful GET bodies to [ResponseCache]
/// and, when a GET fails at the transport level, transparently resolves it with
/// the cached body (marked `extra['fromCache'] = true`) instead of erroring.
///
/// Pairs with the auth interceptor: a post-refresh replay that times out is now
/// rejected as a [DioException], which lands here and is served from cache when
/// available — so a transient overload no longer wipes a card.
class CacheInterceptor extends Interceptor {
  CacheInterceptor(this._cache);

  final ResponseCache _cache;

  bool _cacheable(RequestOptions o) =>
      o.method.toUpperCase() == 'GET' &&
      o.extra['skipAuth'] != true &&
      !o.path.startsWith('/auth');

  /// Stable signature: method + path + sorted query (so param order can't fork
  /// the cache entry).
  String _key(RequestOptions o) {
    final params =
        o.queryParameters.entries.map((e) => '${e.key}=${e.value}').toList()
          ..sort();
    return 'GET ${o.path}?${params.join('&')}';
  }

  @override
  void onResponse(Response response, ResponseInterceptorHandler handler) {
    final o = response.requestOptions;
    final status = response.statusCode ?? 0;
    if (_cacheable(o) && status >= 200 && status < 300) {
      // Fire-and-forget: never block delivery on a disk write.
      unawaited(_cache.write(_key(o), response.data));
    }
    handler.next(response);
  }

  @override
  Future<void> onError(
    DioException err,
    ErrorInterceptorHandler handler,
  ) async {
    final o = err.requestOptions;
    if (_cacheable(o) && _isTransport(err)) {
      final cached = await _cache.read(_key(o));
      if (cached != null) {
        Log.breadcrumb('served from cache ${o.path}', category: 'cache');
        handler.resolve(
          Response(
            requestOptions: o,
            data: cached,
            statusCode: 200,
            extra: {...o.extra, 'fromCache': true},
          ),
        );
        return;
      }
    }
    handler.next(err);
  }

  /// Only fall back where stale data beats an error: timeouts, dropped
  /// connections, and 5xx. A 4xx is a real answer and must surface as-is.
  bool _isTransport(DioException e) {
    switch (e.type) {
      case DioExceptionType.connectionTimeout:
      case DioExceptionType.sendTimeout:
      case DioExceptionType.receiveTimeout:
      case DioExceptionType.connectionError:
      case DioExceptionType.unknown:
        return true;
      case DioExceptionType.badResponse:
        return (e.response?.statusCode ?? 0) >= 500;
      default:
        return false;
    }
  }
}
