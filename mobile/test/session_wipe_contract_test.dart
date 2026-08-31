import 'dart:io';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/providers/auth_controller.dart';

/// Architecture test for the "one wipe coordinator" rule.
///
/// A session is not just a token pair: it is the credential record, the cached
/// profile, the on-disk response cache and the in-memory auth state. Every one
/// of those used to be cleared by whichever call site happened to remember it,
/// and they disagreed — explicit sign-out cleared the response cache but left
/// the tokens to a repository, session expiry cleared the cache but left the
/// tokens *and* the profile on disk.
///
/// The fix is only a fix while it stays true, and "everyone remembers to call
/// the coordinator" is exactly the property that rots silently. So: grep the
/// tree. If a new call site clears one of these stores directly, this fails and
/// names the file.

/// Matches a direct clear of one of the session stores.
final _directClear = RegExp(
    r'(?:tokenStorageProvider\)|responseCacheProvider\)|_storage|_cache|storage)'
    r'\s*\.\s*clear\(\)');

/// Files allowed to contain one, and why.
const _allowed = <String, String>{
  // Owns clear() itself, and discards a credential record it cannot read.
  'lib/src/core/token_storage.dart': 'defines the participant',
  // Owns clear() itself.
  'lib/src/core/response_cache.dart': 'defines the participant',
  // Registers the participants with SessionWipe — checked further below.
  'lib/src/providers/auth_controller.dart': 'the wipe registry'
};

/// The participants a session teardown must clear, in the order they run.
/// In-memory state first, so the router leaves the authenticated shell before
/// the asynchronous disk clearing starts.
const _expectedParticipants = <String>[
  'session-state',
  'credentials',
  'response-cache'
];

void main() {
  final libDir = Directory('lib');

  test('no code outside the wipe registry clears a session store directly', () {
    final offenders = <String>[];
    for (final entity in libDir.listSync(recursive: true)) {
      if (entity is! File || !entity.path.endsWith('.dart')) continue;
      final relative = entity.path.replaceAll(r'\', '/');
      if (_allowed.containsKey(relative)) continue;
      final source = entity.readAsStringSync();
      for (final line in source.split('\n')) {
        if (_directClear.hasMatch(line)) {
          offenders.add('$relative: ${line.trim()}');
        }
      }
    }

    expect(offenders, isEmpty,
        reason:
            'Clear the session through SessionWipe.wipe(), not one store at '
            'a time — a subset clear is how sign-out and session expiry drifted '
            'apart. Offenders:\n${offenders.join('\n')}');
  });

  test('the detector actually bites', () {
    // A guard checked against an empty set passes for the wrong reason. Prove
    // the pattern still recognises the shapes it is supposed to catch.
    const shapes = [
      'await ref.read(tokenStorageProvider).clear();',
      'await _ref.read(responseCacheProvider).clear();',
      'await _storage.clear();',
      'await _cache.clear();',
      'await storage.clear();'
    ];
    for (final shape in shapes) {
      expect(_directClear.hasMatch(shape), isTrue, reason: shape);
    }
    // …and does not fire on unrelated clears.
    expect(_directClear.hasMatch('_lockReturnLocation = null;'), isFalse);
    expect(_directClear.hasMatch('readNotifications.clear();'), isFalse);
  });

  test('every direct clear in the registry file is inside sessionWipeProvider',
      () {
    final source =
        File('lib/src/providers/auth_controller.dart').readAsStringSync();
    final start = source.indexOf('final sessionWipeProvider');
    expect(start, greaterThan(-1), reason: 'the registry must exist');
    // The provider ends at the next top-level `final <name>Provider` line.
    final rest = source.substring(start + 1);
    final nextTopLevel = rest.indexOf('\nfinal ');
    final registry =
        nextTopLevel == -1 ? rest : rest.substring(0, nextTopLevel);
    final outside = nextTopLevel == -1 ? '' : rest.substring(nextTopLevel);

    expect(_directClear.hasMatch(registry), isTrue,
        reason: 'the registry is where the clears live');
    for (final line in (source.substring(0, start) + outside).split('\n')) {
      expect(_directClear.hasMatch(line), isFalse,
          reason: 'clear outside the wipe registry: ${line.trim()}');
    }
  });

  test('the registry covers every part of a session, in order', () {
    final container = ProviderContainer();
    addTearDown(container.dispose);

    expect(container.read(sessionWipeProvider).participants.toList(),
        _expectedParticipants,
        reason: 'a new kind of session state is registered here, not audited '
            'into every call site');
  });
}
