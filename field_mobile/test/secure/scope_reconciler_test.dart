import 'dart:convert';
import 'dart:io';

import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/secure/data_scope.dart';
import 'package:dotmac_field/core/secure/scope_key_ring.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:path/path.dart' as p;

import '../helpers/fake_http.dart';
import '../helpers/secure_store.dart';

void main() {
  test('data belonging to another principal is destroyed at startup', () async {
    final device = newTestDevice();
    final stranger = await device.open(techTwo);
    await stranger.evidence.write(
      'shot.evidence',
      utf8.encode('somebody else evidence'),
      purpose: 'photo',
      reference: 'ref-1',
    );
    expect(stranger.root.existsSync(), isTrue);

    final result = await device.reconciler.reconcile(techOne);

    expect(result.destroyedScopes, [techTwo.key]);
    expect(result.isClean, isFalse);
    expect(stranger.root.existsSync(), isFalse);
    expect(
      await device.vault.read(ScopeKeyRing.databaseKeyName(techTwo.key)),
      isNull,
    );
  });

  test('a scope directory with no key is destroyed, not read', () async {
    final device = newTestDevice();
    // Key material gone (a restored backup, a cleared keychain), files left.
    final orphan = Directory(
      p.join(device.documents.path, 'scopes', techTwo.key),
    )..createSync(recursive: true);
    File(p.join(orphan.path, 'field.sqlite')).writeAsStringSync('bytes');

    final result = await device.reconciler.reconcile(techOne);

    expect(result.destroyedScopes, [techTwo.key]);
    expect(orphan.existsSync(), isFalse);
  });

  test('key material with no directory is destroyed too', () async {
    final device = newTestDevice();
    // Keys without a directory: an uninstall that left the keychain behind.
    await device.keyRing.loadOrCreate(techTwo);

    await device.reconciler.reconcile(techOne);

    expect(await device.keyRing.knownScopeKeys(), isNot(contains(techTwo.key)));
  });

  test('the signed-in principal keeps their own data', () async {
    final device = newTestDevice();
    final mine = await device.open(techOne);
    final evidence = await mine.evidence.write(
      'shot.evidence',
      utf8.encode('my evidence'),
      purpose: 'photo',
      reference: 'ref-1',
    );

    final result = await device.reconciler.reconcile(techOne);

    expect(result.isClean, isTrue);
    expect(evidence.existsSync(), isTrue);
    expect(
      await device.vault.read(ScopeKeyRing.databaseKeyName(techOne.key)),
      isNotNull,
    );
  });

  test('the sweep does not sign the current technician out', () async {
    final device = newTestDevice();
    await device.tokenStore.save(
      accessToken: fakeJwt(
        expiry: DateTime.now().toUtc().add(const Duration(hours: 1)),
        sub: 'tech-1',
      ),
      refreshToken: 'r',
      loginMode: LoginMode.staff,
    );
    await device.open(techTwo);

    await device.reconciler.reconcile(techOne);

    expect(
      await device.tokenStore.accessToken,
      isNotNull,
      reason: "clearing a stranger's data must not end our own session",
    );
  });

  test('a launch with no session sweeps every scope on the device', () async {
    final device = newTestDevice();
    await device.open(techOne);
    await device.open(techTwo);

    final result = await device.reconciler.reconcile(DataScope.unbound);

    expect(result.destroyedScopes.toSet(), {techOne.key, techTwo.key});
    expect(await device.keyRing.knownScopeKeys(), isEmpty);
  });
}
