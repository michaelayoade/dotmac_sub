import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:pointycastle/export.dart'
    show AEADParameters, AESEngine, GCMBlockCipher, KeyParameter;

import 'observability.dart';

/// Authenticated encryption for everything this app writes to the ordinary
/// filesystem.
///
/// The on-disk response cache used to hold plaintext JSON, justified by "tokens
/// are never in a response body". That reasoning was wrong: the bodies we cache
/// are subscriptions, invoices, balances and profiles — the customer's data is
/// the thing worth protecting, not just the credential that fetches it. On a
/// rooted/jailbroken device, a device shared with a repair shop, or an
/// unencrypted backup, that directory was readable.
///
/// So: AES-256-GCM, with a key generated once per install and held in the
/// platform secure store (Keychain / EncryptedSharedPreferences) — the same
/// place the session tokens live. The cache directory on its own is then inert
/// bytes. Deleting the key is a hard delete of everything ever written under
/// it: there is deliberately no way to re-derive it, so [rotate] is how a wipe
/// guarantees that an entry left behind by an interrupted directory delete can
/// never be read again.
///
/// GCM's associated data carries the *scope* of the entry (see
/// `MobileDataScope`). Moving a file from one account's cache directory into
/// another's does not make it readable — the tag check fails, and a failed tag
/// check is a cache miss, never an error the user sees.
class CacheCipher {
  CacheCipher([FlutterSecureStorage? storage])
      : _storage = storage ??
            const FlutterSecureStorage(
                aOptions: AndroidOptions(encryptedSharedPreferences: true));

  final FlutterSecureStorage _storage;

  static const _kCacheKey = 'cache_encryption_key_v1';

  /// Envelope marker. Lets [open] reject anything that is not one of ours —
  /// notably the plaintext `.json` files written by the previous cache format —
  /// before it wastes a tag check on it.
  static const _magic = <int>[0x44, 0x4d, 0x43, 0x31]; // "DMC1"
  static const _nonceLength = 12; // 96-bit nonce: the GCM-recommended size.
  static const _tagBits = 128;
  static const _keyLength = 32; // AES-256

  /// Read once and held: the secure store is a platform channel and the cache
  /// sits on the request path. Cleared by [rotate].
  Uint8List? _key;
  Future<Uint8List?>? _loading;

  final Random _random = Random.secure();

  /// Encrypt [plaintext], binding it to [aad]. Returns null when no key could
  /// be obtained (secure storage unavailable) — the caller must then write
  /// nothing at all rather than fall back to plaintext.
  Future<Uint8List?> seal(List<int> plaintext, {required String aad}) async {
    final key = await _obtainKey(createIfMissing: true);
    if (key == null) return null;
    try {
      final nonce = _randomBytes(_nonceLength);
      final cipher = GCMBlockCipher(AESEngine())
        ..init(
            true,
            AEADParameters(KeyParameter(key), _tagBits, nonce,
                Uint8List.fromList(utf8.encode(aad))));
      final sealed = cipher.process(Uint8List.fromList(plaintext));
      final out = Uint8List(_magic.length + nonce.length + sealed.length);
      out.setRange(0, _magic.length, _magic);
      out.setRange(_magic.length, _magic.length + nonce.length, nonce);
      out.setRange(_magic.length + nonce.length, out.length, sealed);
      return out;
    } catch (e) {
      Log.breadcrumb('cache seal failed: ${Log.describeError(e)}',
          category: 'cache');
      return null;
    }
  }

  /// Decrypt an envelope produced by [seal] under the same [aad]. Returns null
  /// for *every* failure mode — no key, wrong key, wrong scope, truncated file,
  /// legacy plaintext — because at this layer they are all the same thing: a
  /// cache miss. Failing closed is the point; a stale-fallback cache must never
  /// turn into a visible error.
  Future<Uint8List?> open(List<int> sealed, {required String aad}) async {
    // Never create a key on the read path: if the key is gone, everything
    // previously written under it must stay unreadable forever.
    final key = await _obtainKey(createIfMissing: false);
    if (key == null) return null;
    if (sealed.length <= _magic.length + _nonceLength) return null;
    for (var i = 0; i < _magic.length; i++) {
      if (sealed[i] != _magic[i]) return null;
    }
    try {
      final nonce = Uint8List.fromList(
          sealed.sublist(_magic.length, _magic.length + _nonceLength));
      final body =
          Uint8List.fromList(sealed.sublist(_magic.length + _nonceLength));
      final cipher = GCMBlockCipher(AESEngine())
        ..init(
            false,
            AEADParameters(KeyParameter(key), _tagBits, nonce,
                Uint8List.fromList(utf8.encode(aad))));
      return cipher.process(body);
    } catch (_) {
      // Tag mismatch (tampered, wrong scope, or wrong key) — a miss.
      return null;
    }
  }

  /// Destroy the key. Every entry ever sealed under it becomes permanently
  /// unreadable, including any file a crash-interrupted directory delete left
  /// behind. Called as part of the session wipe.
  Future<void> rotate() async {
    _key = null;
    _loading = null;
    try {
      await _storage.delete(key: _kCacheKey);
    } catch (e) {
      Log.breadcrumb('cache key delete failed: ${Log.describeError(e)}',
          category: 'cache');
    }
  }

  /// Single-flight: the request path can ask for the key from several
  /// concurrent cache reads, and they must share one secure-store round trip.
  Future<Uint8List?> _obtainKey({required bool createIfMissing}) async {
    final held = _key;
    if (held != null) return held;
    final pending = _loading ??= _loadKey(createIfMissing: createIfMissing);
    try {
      return await pending;
    } finally {
      if (identical(_loading, pending)) _loading = null;
    }
  }

  Future<Uint8List?> _loadKey({required bool createIfMissing}) async {
    try {
      final existing = await _storage.read(key: _kCacheKey);
      if (existing != null && existing.isNotEmpty) {
        final decoded = base64Decode(existing);
        if (decoded.length == _keyLength) return _key = decoded;
        // A key of the wrong length is unusable; treat it as absent rather than
        // silently producing garbage.
      }
      if (!createIfMissing) return null;
      final fresh = _randomBytes(_keyLength);
      await _storage.write(key: _kCacheKey, value: base64Encode(fresh));
      return _key = fresh;
    } catch (e) {
      // Secure storage unavailable (no platform channel in a plain Dart test,
      // a locked keystore). No key means no cache — never plaintext.
      Log.breadcrumb('cache key unavailable: ${Log.describeError(e)}',
          category: 'cache');
      return null;
    }
  }

  Uint8List _randomBytes(int length) {
    final out = Uint8List(length);
    for (var i = 0; i < length; i++) {
      out[i] = _random.nextInt(256);
    }
    return out;
  }
}
