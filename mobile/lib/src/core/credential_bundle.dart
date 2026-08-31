import 'dart:convert';

import 'data_scope.dart';

/// Why a persisted credential record could not be turned into a [CredentialBundle].
enum CredentialDecodeOutcome {
  /// A usable record of a version we understand.
  ok,

  /// Nothing stored under the bundle key (a fresh install, or a wipe).
  empty,

  /// Present but not parseable / missing a required field — a truncated or
  /// corrupted write.
  unreadable,

  /// A schema version this build does not know how to read. Almost always a
  /// downgrade (the user rolled back an app version).
  unsupportedVersion
}

/// The result of decoding the stored credential record.
///
/// Decoding is deliberately all-or-nothing: the caller either gets a complete
/// [bundle] or gets told to discard the record wholesale. There is no path that
/// applies *some* of a record we do not fully understand — a half-applied
/// credential record is exactly the mismatched access/refresh pair this bundle
/// exists to make impossible.
class CredentialDecode {
  const CredentialDecode._(this.outcome, this.bundle);

  final CredentialDecodeOutcome outcome;
  final CredentialBundle? bundle;

  bool get isOk => outcome == CredentialDecodeOutcome.ok;

  /// True when the stored bytes exist but must be thrown away (rather than
  /// simply being absent). The caller wipes on this.
  bool get mustDiscard =>
      outcome == CredentialDecodeOutcome.unreadable ||
      outcome == CredentialDecodeOutcome.unsupportedVersion;
}

/// The whole local session, as one serialized record.
///
/// Before this existed the access and refresh tokens were two independent
/// secure-store keys written one after the other. A crash, a kill, or a
/// low-memory eviction between those two writes left a *mismatched pair* on
/// disk — an access token from one issue and a refresh token from the previous
/// one — which the app then happily used until the backend rejected both. One
/// record written in one call cannot tear that way: either the whole session
/// lands or none of it does.
///
/// The record also carries the things a bare token pair could never answer:
///  * [scope] — whose session this is, so cached data can be partitioned and a
///    resumed session can be attributed without waiting for `/auth/me`;
///  * [generation] — the session-fencing stamp (see `SessionFence`), which is
///    what makes "logout racing an in-flight refresh" safe; a writer carrying
///    an older generation is refused rather than resurrecting the session.
class CredentialBundle {
  const CredentialBundle(
      {required this.accessToken,
      required this.refreshToken,
      required this.scope,
      required this.generation,
      required this.issuedAt,
      this.schemaVersion = currentVersion});

  /// Bump only for a change that older builds could not read correctly. A
  /// purely additive optional field does not need a bump (see [decode], which
  /// ignores unknown keys).
  static const int currentVersion = 2;

  /// The oldest version this build can still read. Version 1 was not a bundle
  /// at all — it was the separate `access_token` / `refresh_token` keys, and is
  /// migrated by `TokenStorage`, not decoded here.
  static const int minimumSupportedVersion = 2;

  final int schemaVersion;
  final String accessToken;
  final String? refreshToken;
  final MobileDataScope scope;
  final int generation;
  final DateTime issuedAt;

  CredentialBundle copyWith(
          {String? accessToken,
          String? refreshToken,
          MobileDataScope? scope,
          DateTime? issuedAt}) =>
      CredentialBundle(
          accessToken: accessToken ?? this.accessToken,
          refreshToken: refreshToken ?? this.refreshToken,
          scope: scope ?? this.scope,
          generation: generation,
          issuedAt: issuedAt ?? this.issuedAt);

  String encode() => jsonEncode({
        'v': schemaVersion,
        'access_token': accessToken,
        if (refreshToken != null) 'refresh_token': refreshToken,
        'tenant': scope.tenant,
        'principal': scope.principal,
        'generation': generation,
        'issued_at': issuedAt.toUtc().toIso8601String()
      });

  static CredentialDecode decode(String? raw) {
    if (raw == null || raw.isEmpty) {
      return const CredentialDecode._(CredentialDecodeOutcome.empty, null);
    }
    Object? parsed;
    try {
      parsed = jsonDecode(raw);
    } catch (_) {
      return const CredentialDecode._(CredentialDecodeOutcome.unreadable, null);
    }
    if (parsed is! Map) {
      return const CredentialDecode._(CredentialDecodeOutcome.unreadable, null);
    }

    final version = parsed['v'];
    if (version is! int) {
      return const CredentialDecode._(CredentialDecodeOutcome.unreadable, null);
    }
    // A record from the future (an app downgrade) may mean something different
    // by the same field names. Refuse it rather than guess.
    if (version < minimumSupportedVersion || version > currentVersion) {
      return const CredentialDecode._(
          CredentialDecodeOutcome.unsupportedVersion, null);
    }

    final access = parsed['access_token'];
    final generation = parsed['generation'];
    // A generation must be positive: 0 is the fence's "no session" value, and
    // a record claiming it would either crash the fence or, worse, be matched
    // by an unfenced writer.
    if (access is! String ||
        access.isEmpty ||
        generation is! int ||
        generation <= 0) {
      return const CredentialDecode._(CredentialDecodeOutcome.unreadable, null);
    }
    final refresh = parsed['refresh_token'];
    final issuedAt = DateTime.tryParse('${parsed['issued_at']}');

    return CredentialDecode._(
        CredentialDecodeOutcome.ok,
        CredentialBundle(
            schemaVersion: version,
            accessToken: access,
            refreshToken:
                refresh is String && refresh.isNotEmpty ? refresh : null,
            scope: MobileDataScope(
                tenant: '${parsed['tenant'] ?? ''}',
                principal: '${parsed['principal'] ?? ''}'),
            generation: generation,
            issuedAt:
                issuedAt ?? DateTime.fromMillisecondsSinceEpoch(0).toUtc()));
  }

  /// Never print the tokens. This type ends up in debug output and breadcrumbs.
  @override
  String toString() =>
      'CredentialBundle(v$schemaVersion, gen $generation, $scope)';
}
