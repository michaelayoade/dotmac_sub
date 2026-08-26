import 'dart:convert';
import 'dart:io';

import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/draft_store.dart';
import 'package:dotmac_field/core/secure/data_scope.dart';
import 'package:dotmac_field/core/secure/evidence_files.dart';
import 'package:dotmac_field/core/secure/offline_wipe.dart';
import 'package:dotmac_field/core/secure/scope_key_ring.dart';
import 'package:dotmac_field/core/secure/secret_vault.dart';
import 'package:dotmac_field/core/secure/secure_field_store.dart';
import 'package:dotmac_field/core/secure/session_lifecycle.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:path/path.dart' as p;

import '../helpers/fake_http.dart';
import '../helpers/secure_store.dart';

/// What a wiped device must look like, whichever trigger did the wiping.
typedef _Aftermath = ({
  bool scopeDirectoryGone,
  bool databaseKeyGone,
  bool evidenceKeyGone,
  bool oldTokensGone,
  bool journalGone,
  bool legacyPlaintextGone,
});

typedef _SessionEnding = ({
  WipeTrigger trigger,
  Future<void> Function(TestDevice device, SessionLifecycle session) run,
});

/// A signed-in technician with unsent evidence, drafts and legacy plaintext on
/// the handset — everything a wipe has to take with it.
typedef _SignedIn = ({
  TestDevice device,
  _RecordingWipe wipe,
  SessionLifecycle session,
  String accessToken,
});

const _wiped = (
  scopeDirectoryGone: true,
  databaseKeyGone: true,
  evidenceKeyGone: true,
  oldTokensGone: true,
  journalGone: true,
  legacyPlaintextGone: true,
);

void main() {
  useHostSqlite3();

  String tokenFor(String subject) => fakeJwt(
    expiry: DateTime.now().toUtc().add(const Duration(hours: 1)),
    sub: subject,
  );

  Future<_Aftermath> aftermath(
    TestDevice device,
    DataScope scope,
    String oldToken,
  ) async => (
    scopeDirectoryGone: !Directory(
      p.join(device.documents.path, 'scopes', scope.key),
    ).existsSync(),
    databaseKeyGone:
        await device.vault.read(ScopeKeyRing.databaseKeyName(scope.key)) ==
        null,
    evidenceKeyGone:
        await device.vault.read(ScopeKeyRing.evidenceKeyName(scope.key)) ==
        null,
    oldTokensGone: await device.tokenStore.accessToken != oldToken,
    journalGone: !device.wipe.journalFile.existsSync(),
    legacyPlaintextGone: !await device.legacyPaths.exists,
  );

  DraftStore draftsIn(SecureFieldStore store) => DraftStore(
    db: store.database,
    cipher: store.cipher,
    scopeKey: store.scopeKey,
  );

  Future<_SignedIn> signIn(String subject) async {
    final device = newTestDevice();
    final accessToken = tokenFor(subject);
    await device.tokenStore.save(
      accessToken: accessToken,
      refreshToken: 'r',
      loginMode: LoginMode.staff,
    );
    final recording = _RecordingWipe(device.wipe);
    final session = device.lifecycle(wipeOverride: recording);
    await session.restore();
    final store = session.store!;
    await draftsIn(store).save(
      id: 'material_request:new',
      type: 'material_request',
      payload: {'note': 'unsent'},
    );
    await store.evidence.write(
      'shot.evidence',
      utf8.encode('a customer photo'),
      purpose: 'photo',
      reference: 'ref-1',
    );
    device.legacyPaths.database.writeAsStringSync('legacy plaintext');
    return (
      device: device,
      wipe: recording,
      session: session,
      accessToken: accessToken,
    );
  }

  /// Awaits a future we expect to either complete or fail, because which of the
  /// two happens depends on where the wipe caught it. What must not vary is the
  /// state it leaves behind.
  Future<void> settle(Future<Object?> future) async {
    try {
      await future;
    } on Object {
      // Both outcomes are acceptable; the assertions are about the aftermath.
    }
  }

  test('logout, revocation and account switch share one wipe', () async {
    final endings = <_SessionEnding>[
      (
        trigger: WipeTrigger.explicitLogout,
        run: (device, session) => session.signOut(),
      ),
      (
        trigger: WipeTrigger.tokenRevoked,
        run: (device, session) => session.sessionRevoked(),
      ),
      (
        trigger: WipeTrigger.accountSwitch,
        run: (device, session) async {
          // A different technician signs in on the same handset.
          await device.tokenStore.save(
            accessToken: tokenFor('tech-2'),
            refreshToken: 'r2',
            loginMode: LoginMode.staff,
          );
          await session.beginSession();
        },
      ),
    ];

    final outcomes = <WipeTrigger, _Aftermath>{};
    final implementations = <Type>{};

    for (final ending in endings) {
      final context = await signIn('tech-1');
      final scope = context.session.store!.scope;

      await ending.run(context.device, context.session);

      expect(
        context.wipe.requests.map((request) => request.trigger),
        [ending.trigger],
        reason: '${ending.trigger.name} must run the wipe exactly once',
      );
      expect(context.wipe.requests.single.scopeKey, scope.key);
      implementations.add(context.wipe.delegate.runtimeType);
      outcomes[ending.trigger] = await aftermath(
        context.device,
        scope,
        context.accessToken,
      );
    }

    expect(
      implementations,
      {ScopedOfflineWipe},
      reason: 'three triggers, one implementation',
    );
    for (final trigger in outcomes.keys) {
      expect(outcomes[trigger], _wiped, reason: 'aftermath of $trigger');
    }
  });

  test('an account switch leaves the new technician an empty store', () async {
    final context = await signIn('tech-1');
    final firstScope = context.session.store!.scope;

    await context.device.tokenStore.save(
      accessToken: tokenFor('tech-2'),
      refreshToken: 'r2',
      loginMode: LoginMode.staff,
    );
    await context.session.beginSession();

    final second = context.session.store;
    expect(second, isNotNull);
    expect(second!.scope, isNot(firstScope));
    expect(await context.device.tokenStore.accessToken, isNotNull);
    expect(await draftsIn(second).list('material_request'), isEmpty);
  });

  test('an interrupted wipe leaves no readable residue', () async {
    final context = await signIn('tech-1');
    final device = context.device;
    final store = context.session.store!;
    final scope = store.scope;
    final evidenceFile = store.evidence.fileNamed('shot.evidence');
    final sealed = evidenceFile.readAsBytesSync();

    await device.wipe.wipe(
      WipeRequest(scopeKey: scope.key, trigger: WipeTrigger.explicitLogout),
    );
    // Recreate exactly what a process killed between step 4 (keys destroyed)
    // and step 5 (files deleted) would have left behind, journal included.
    evidenceFile.parent.createSync(recursive: true);
    evidenceFile.writeAsBytesSync(sealed);
    device.wipe.journalFile.writeAsStringSync(
      jsonEncode([
        WipeRequest(
          scopeKey: scope.key,
          trigger: WipeTrigger.explicitLogout,
        ).toJson(),
      ]),
    );

    // The residue is on disk, and there is no key left anywhere that opens it.
    expect(evidenceFile.existsSync(), isTrue);
    expect(
      (await device.vault.names()).where((name) => name.contains(scope.key)),
      isEmpty,
    );

    final resumed = await device.wipe.resumeInterrupted();

    expect(resumed.map((request) => request.scopeKey), [scope.key]);
    expect(store.root.existsSync(), isFalse);
    expect(device.wipe.journalFile.existsSync(), isFalse);
  });

  test('a write in flight at logout cannot recreate the store', () async {
    final context = await signIn('tech-1');
    final store = context.session.store!;

    // A photo write and a draft write are both suspended mid-flight.
    final photo = store.evidence.write(
      'late.evidence',
      utf8.encode('captured a heartbeat before logout'),
      purpose: 'photo',
      reference: 'ref-late',
    );
    final draft = draftsIn(store).save(
      id: 'expense_request:new',
      type: 'expense_request',
      payload: {'note': 'typed a heartbeat before logout'},
    );

    await context.session.signOut();
    await settle(photo);
    await settle(draft);

    // Whatever those writers were doing, anything they try next is refused.
    await expectLater(
      store.evidence.write(
        'later.evidence',
        utf8.encode('after'),
        purpose: 'photo',
        reference: 'ref-later',
      ),
      throwsA(isA<StoreDiscarded>()),
    );
    await pumpEventQueue();

    expect(store.root.existsSync(), isFalse);
    expect(
      await aftermath(context.device, store.scope, context.accessToken),
      _wiped,
    );
  });

  test('the auth controller routes both endings into the lifecycle', () async {
    // The portal-facing triggers are a controller call away from the wipe; this
    // is where a second "just forget the token" path would show up.
    final context = await signIn('tech-1');
    final container = ProviderContainer(
      overrides: [
        tokenStoreProvider.overrideWithValue(context.device.tokenStore),
        sessionLifecycleProvider.overrideWithValue(context.session),
      ],
    );
    addTearDown(container.dispose);
    final controller = container.read(authControllerProvider.notifier);

    await controller.logout();
    expect(context.wipe.requests.map((request) => request.trigger), [
      WipeTrigger.explicitLogout,
    ]);

    await controller.sessionExpired();
    expect(context.wipe.requests.map((request) => request.trigger), [
      WipeTrigger.explicitLogout,
      WipeTrigger.tokenRevoked,
    ]);
  });

  test('the keys are destroyed before the files they protect', () async {
    final context = await signIn('tech-1');
    final store = context.session.store!;
    final observations = <String>[];
    final observed = _ObservingVault(
      context.device.vault,
      onDelete: () => observations.add(
        store.root.existsSync() ? 'files-still-there' : 'files-gone',
      ),
    );
    final wipe = ScopedOfflineWipe(
      documents: context.device.documents,
      keyRing: ScopeKeyRing(observed),
      tokenStore: context.device.tokenStore,
      legacyPaths: context.device.legacyPaths,
    );

    await wipe.wipe(
      WipeRequest(
        scopeKey: store.scopeKey,
        trigger: WipeTrigger.explicitLogout,
      ),
    );

    expect(observations, isNotEmpty);
    expect(observations.toSet(), {'files-still-there'});
    expect(store.root.existsSync(), isFalse);
  });
}

/// Wraps the one wipe implementation so a test can see every request without
/// substituting a second implementation for it.
class _RecordingWipe implements OfflineWipe {
  _RecordingWipe(this.delegate);

  final OfflineWipe delegate;
  final List<WipeRequest> requests = [];

  @override
  Future<void> wipe(WipeRequest request, {SecureFieldStore? live}) {
    requests.add(request);
    return delegate.wipe(request, live: live);
  }

  @override
  Future<List<WipeRequest>> resumeInterrupted() => delegate.resumeInterrupted();
}

class _ObservingVault implements SecretVault {
  _ObservingVault(this._inner, {required this.onDelete});

  final SecretVault _inner;
  final void Function() onDelete;

  @override
  Future<String?> read(String key) => _inner.read(key);

  @override
  Future<void> write(String key, String value) => _inner.write(key, value);

  @override
  Future<void> delete(String key) {
    onDelete();
    return _inner.delete(key);
  }

  @override
  Future<Set<String>> names() => _inner.names();
}
