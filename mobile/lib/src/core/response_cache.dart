import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:path_provider/path_provider.dart';

import 'cache_crypto.dart';
import 'data_scope.dart';
import 'observability.dart';
import 'session_fence.dart';

/// On-disk cache of the last successful JSON body for idempotent GETs, used as a
/// *stale fallback* when the network fails (timeout / connection reset / 5xx /
/// offline). It lets a card render its last-known value during a server-overload
/// blip instead of flipping to an error.
///
/// Two things about this cache were wrong and are now fixed here.
///
/// **It was plaintext.** The old justification was "tokens live in the secure
/// store and are never part of a response body, so nothing secret is written
/// here". That is the wrong test. What we cache is subscriptions, invoices,
/// balances, tickets and profiles — the customer's data is the asset, not just
/// the credential that fetches it. Bodies are now sealed with AES-256-GCM under
/// a per-install key held in the platform secure store (see [CacheCipher]).
///
/// **It was unpartitioned.** The cache key was method + path + query, with no
/// account in it, and the whole directory was flat. Two accounts on one device
/// — or a reseller and the customer they are viewing as — addressed the *same
/// file* for the same request, and correctness rested entirely on "we clear it
/// on logout", which is a promise, not a mechanism (and a promise that a crash
/// between sign-out and the delete breaks). Every entry now lives under its
/// [MobileDataScope]: the scope is in the directory, in the entry name, and in
/// the AEAD associated data. Another account cannot *address* an entry, and a
/// file physically moved into its directory fails the tag check.
class ResponseCache {
  ResponseCache(
      {Directory? directory,
      Future<Directory> Function()? openDir,
      CacheCipher? cipher})
      : _injected = directory,
        _root = directory,
        _openDir = openDir ?? _defaultDir,
        _cipher = cipher ?? CacheCipher();

  /// A directory supplied by the caller (tests). Held separately from [_root]
  /// so [clear], which resets the resolved root, can restore it rather than
  /// silently falling back to the platform app-support directory.
  final Directory? _injected;
  final Future<Directory> Function() _openDir;
  final CacheCipher _cipher;

  Directory? _root;
  Future<Directory>? _opening;
  Directory? _scopeDir;
  bool _purgedLegacy = false;

  MobileDataScope _scope = MobileDataScope.anonymous;

  /// The partition reads and writes currently address.
  MobileDataScope get scope => _scope;

  /// Point the cache at a different identity. Called when a session resolves
  /// (`/auth/me`), when a reseller enters or leaves "view as customer", and on
  /// wipe. Entries written under the previous scope are neither readable nor
  /// addressable from the new one — no filtering involved.
  void useScope(MobileDataScope scope) {
    if (scope == _scope) return;
    _scope = scope;
    _scopeDir = null;
  }

  static Future<Directory> _defaultDir() async {
    final base = await getApplicationSupportDirectory();
    final dir = Directory('${base.path}/api_cache');
    if (!await dir.exists()) {
      await dir.create(recursive: true);
    }
    return dir;
  }

  Future<Directory> _ensureRoot() async {
    final root = _root ??= await (_opening ??= _openDir());
    if (!_purgedLegacy) {
      _purgedLegacy = true;
      await _purgeLegacyPlaintext(root);
    }
    return root;
  }

  /// Upgrade hygiene: the previous cache format wrote unencrypted `*.json`
  /// straight into the root. Those files are not readable by the new format
  /// (they fail the envelope check), but "unreadable by us" is not "deleted" —
  /// they are still plaintext customer data sitting on the device. Remove them
  /// once, on first use after the upgrade.
  Future<void> _purgeLegacyPlaintext(Directory root) async {
    try {
      if (!await root.exists()) return;
      await for (final entry in root.list(followLinks: false)) {
        if (entry is File && entry.path.endsWith('.json')) {
          await entry.delete();
        }
      }
    } catch (e) {
      Log.breadcrumb('legacy cache purge failed: ${Log.describeError(e)}',
          category: 'cache');
    }
  }

  Future<Directory> _ensureScopeDir() async {
    final held = _scopeDir;
    if (held != null) return held;
    final dir = Directory('${(await _ensureRoot()).path}/s_${_scope.segment}');
    if (!await dir.exists()) {
      await dir.create(recursive: true);
    }
    return _scopeDir = dir;
  }

  /// Filesystem-safe name for a request signature within the current scope.
  ///
  /// The readable prefix is kept for debuggability, but the part that makes the
  /// name unique is a digest over *scope + key*, not over the key alone: the
  /// same request under a different account resolves to a different file, so
  /// one account cannot name another's entry even by constructing the key by
  /// hand.
  String _fileName(String key) {
    final safe = key.replaceAll(RegExp(r'[^A-Za-z0-9._-]'), '_');
    final capped = safe.length <= 80 ? safe : safe.substring(0, 80);
    return '${capped}_${_scope.tagFor(key)}.bin';
  }

  Future<File> _file(String key) async =>
      File('${(await _ensureScopeDir()).path}/${_fileName(key)}');

  Future<void> write(String key, Object? data) async {
    if (data == null) return;
    try {
      final sealed = await _cipher.seal(utf8.encode(jsonEncode(data)),
          aad: _scope.segment);
      // No key material means no cache. Never degrade to plaintext.
      if (sealed == null) return;
      await (await _file(key)).writeAsBytes(sealed, flush: true);
    } catch (e) {
      // A cache write must never break a request.
      Log.breadcrumb('cache write failed: ${Log.describeError(e)}',
          category: 'cache');
    }
  }

  Future<Object?> read(String key) async {
    try {
      final file = await _file(key);
      if (!await file.exists()) return null;
      final plain =
          await _cipher.open(await file.readAsBytes(), aad: _scope.segment);
      // Fail closed: a missing/rotated key, a tampered file, or an entry from
      // another scope is a cache miss, not an error the user ever sees.
      if (plain == null) return null;
      return jsonDecode(utf8.decode(plain));
    } catch (e) {
      Log.breadcrumb('cache read failed: ${Log.describeError(e)}',
          category: 'cache');
      return null;
    }
  }

  /// Drop every entry, in every scope, and destroy the encryption key.
  ///
  /// Rotating the key is what makes this a *guarantee* rather than a best
  /// effort: if the recursive delete is interrupted (a kill mid-wipe, a file
  /// held open), whatever survives was sealed under a key that no longer
  /// exists anywhere and can never be opened again.
  ///
  /// Call this through `SessionWipe`, not directly.
  Future<void> clear() async {
    try {
      final root = await _ensureRoot();
      if (await root.exists()) await root.delete(recursive: true);
    } catch (_) {
      // Best-effort; a failed clear is not worth surfacing — the key rotation
      // below is the part that actually has to hold.
    } finally {
      _root = _injected;
      _opening = null;
      _scopeDir = null;
      _purgedLegacy = false;
    }
    await _cipher.rotate();
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
  CacheInterceptor(this._cache, {this.onCacheState, this.fence});

  final ResponseCache _cache;

  /// Notified with `true` when a GET is served from the stale cache (the
  /// network failed) and `false` when a GET completes fresh from the network.
  /// Lets the UI show a subtle "offline — showing last saved data" banner.
  final void Function(bool fromCache)? onCacheState;

  /// Session fence. A response that comes back after the session it was issued
  /// under ended must not be written to disk: by then the wipe has already run,
  /// and the write would re-create cached customer data on a signed-out device.
  /// Null (the default) leaves the interceptor unfenced, for the cache tests
  /// that exercise it without an auth stack.
  final SessionFence? fence;

  bool _cacheable(RequestOptions o) =>
      o.method.toUpperCase() == 'GET' &&
      o.extra['skipAuth'] != true &&
      !o.path.startsWith('/auth');

  /// Stable signature: method + path + sorted query (so param order can't fork
  /// the cache entry). The identity is *not* part of this string — it is
  /// applied by [ResponseCache], which owns the partition.
  String _key(RequestOptions o) {
    final params = o.queryParameters.entries
        .map((e) => '${e.key}=${e.value}')
        .toList()
      ..sort();
    return 'GET ${o.path}?${params.join('&')}';
  }

  /// Whether a response may still be written: the session it belongs to must
  /// still be the current one, and the cache must still be pointed at the scope
  /// the request was issued under.
  bool _stillCurrent(RequestOptions o) {
    final f = fence;
    if (f != null && !f.holds(o.extra['sessionGeneration'])) return false;
    final issuedScope = o.extra['scopeSegment'];
    if (issuedScope is String && issuedScope != _cache.scope.segment) {
      return false;
    }
    return true;
  }

  @override
  void onResponse(Response response, ResponseInterceptorHandler handler) {
    final o = response.requestOptions;
    final status = response.statusCode ?? 0;
    if (_cacheable(o) && status >= 200 && status < 300 && _stillCurrent(o)) {
      // Fire-and-forget: never block delivery on a disk write.
      unawaited(_cache.write(_key(o), response.data));
      // A fresh network response clears any "offline" state.
      if (response.extra['fromCache'] != true) onCacheState?.call(false);
    }
    handler.next(response);
  }

  @override
  Future<void> onError(
      DioException err, ErrorInterceptorHandler handler) async {
    final o = err.requestOptions;
    if (_cacheable(o) && _isTransport(err) && _stillCurrent(o)) {
      final cached = await _cache.read(_key(o));
      if (cached != null) {
        Log.breadcrumb('served from cache ${o.path}', category: 'cache');
        onCacheState?.call(true);
        handler.resolve(Response(
            requestOptions: o,
            data: cached,
            statusCode: 200,
            extra: {...o.extra, 'fromCache': true}));
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
