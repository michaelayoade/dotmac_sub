import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';

import 'package:pointycastle/export.dart';

/// Raised when an envelope cannot be opened: wrong key, wrong scope, truncated
/// file, or tampered bytes. Never distinguishes between them — a caller that
/// could tell "wrong key" from "tampered" would be an oracle.
class EvidenceCipherFailure implements Exception {
  const EvidenceCipherFailure(this.reason);

  final String reason;

  @override
  String toString() => 'EvidenceCipherFailure: $reason';
}

/// AES-256-GCM envelopes for everything a copied file or a copied database row
/// could otherwise reveal: photos, signatures, drafts and the queued location
/// payloads.
///
/// Layout is `magic | version | nonce(12) | tag(16) | ciphertext`. The
/// authenticated-data field binds each envelope to its [context] — the scope
/// key plus what the bytes are for — so an envelope lifted out of one
/// technician's storage cannot be replayed into another's even if both keys
/// were somehow known.
class EvidenceCipher {
  EvidenceCipher(Uint8List key, {Random? random})
    : _key = KeyParameter(Uint8List.fromList(key)),
      _random = random ?? Random.secure() {
    if (key.length != keyLength) {
      throw ArgumentError.value(key.length, 'key', 'expected $keyLength bytes');
    }
  }

  /// AES-256.
  static const keyLength = 32;
  static const _nonceLength = 12;
  static const _tagBits = 128;
  static const _tagLength = _tagBits ~/ 8;
  static const _magic = <int>[0x44, 0x4d, 0x45, 0x56]; // 'DMEV'
  static const _version = 1;
  static const _headerLength = 5 + _nonceLength + _tagLength;

  final KeyParameter _key;
  final Random _random;

  /// Generates fresh key material. Uses [Random.secure], which is the
  /// platform CSPRNG — never a seeded [Random].
  static Uint8List newKey({Random? random}) {
    final source = random ?? Random.secure();
    return Uint8List.fromList(
      List<int>.generate(keyLength, (_) => source.nextInt(256)),
    );
  }

  Uint8List seal(List<int> plaintext, {required String context}) {
    final nonce = Uint8List.fromList(
      List<int>.generate(_nonceLength, (_) => _random.nextInt(256)),
    );
    final cipher = GCMBlockCipher(AESEngine())
      ..init(true, AEADParameters(_key, _tagBits, nonce, _aad(context)));
    // PointyCastle appends the tag to the ciphertext; we store it in the
    // header so the layout is fixed-offset and a truncated file is detectable
    // before any decryption work happens.
    final sealed = cipher.process(Uint8List.fromList(plaintext));
    final body = sealed.sublist(0, sealed.length - _tagLength);
    final tag = sealed.sublist(sealed.length - _tagLength);
    final envelope = BytesBuilder(copy: false)
      ..add(_magic)
      ..addByte(_version)
      ..add(nonce)
      ..add(tag)
      ..add(body);
    return envelope.toBytes();
  }

  Uint8List open(List<int> envelope, {required String context}) {
    final bytes = Uint8List.fromList(envelope);
    if (bytes.length < _headerLength) {
      throw const EvidenceCipherFailure('envelope truncated');
    }
    for (var i = 0; i < _magic.length; i++) {
      if (bytes[i] != _magic[i]) {
        throw const EvidenceCipherFailure('not an evidence envelope');
      }
    }
    if (bytes[_magic.length] != _version) {
      throw const EvidenceCipherFailure('unsupported envelope version');
    }
    final nonce = bytes.sublist(5, 5 + _nonceLength);
    final tag = bytes.sublist(5 + _nonceLength, _headerLength);
    final body = bytes.sublist(_headerLength);
    final cipher = GCMBlockCipher(AESEngine())
      ..init(false, AEADParameters(_key, _tagBits, nonce, _aad(context)));
    try {
      return cipher.process(Uint8List.fromList(<int>[...body, ...tag]));
    } on Object {
      throw const EvidenceCipherFailure('envelope failed authentication');
    }
  }

  /// Convenience wrappers for the text columns (drafts, outbox payloads) that
  /// hold an envelope rather than readable JSON.
  String sealText(String plaintext, {required String context}) =>
      base64Encode(seal(utf8.encode(plaintext), context: context));

  String openText(String envelope, {required String context}) {
    final Uint8List raw;
    try {
      raw = base64Decode(envelope);
    } on FormatException {
      throw const EvidenceCipherFailure('envelope is not base64');
    }
    return utf8.decode(open(raw, context: context));
  }

  Uint8List _aad(String context) =>
      Uint8List.fromList(<int>[..._magic, _version, ...utf8.encode(context)]);
}
