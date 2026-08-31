import 'dart:io';

import 'package:path/path.dart' as p;

import 'data_scope.dart';
import 'offline_wipe.dart';
import 'scope_key_ring.dart';

class ScopeReconciliation {
  const ScopeReconciliation({required this.destroyedScopes});

  /// Scope keys whose data did not belong to the signed-in principal and was
  /// destroyed. Empty on a healthy device.
  final List<String> destroyedScopes;

  bool get isClean => destroyedScopes.isEmpty;
}

/// Startup sweep for data that does not belong to the principal now holding the
/// device.
///
/// Two independent sources are checked, because either one alone can be the
/// leftover: a scope directory with no key, and key material with no directory.
/// Anything whose scope is not the current one is destroyed through the same
/// journalled path a logout uses — never quarantined into a readable corner,
/// and never left where a later query could reach it.
class ScopeReconciler {
  ScopeReconciler({
    required this.documents,
    required this.keyRing,
    required this.wipe,
  });

  final Directory documents;
  final ScopeKeyRing keyRing;
  final OfflineWipe wipe;

  Directory get scopesRoot => Directory(p.join(documents.path, 'scopes'));

  Future<ScopeReconciliation> reconcile(DataScope current) async {
    final known = <String>{...await keyRing.knownScopeKeys()};
    if (await scopesRoot.exists()) {
      await for (final entry in scopesRoot.list()) {
        if (entry is Directory) known.add(p.basename(entry.path));
      }
    }
    final foreign = known.where((scope) => scope != current.key).toList()
      ..sort();
    for (final scope in foreign) {
      await wipe.wipe(
        WipeRequest(scopeKey: scope, trigger: WipeTrigger.orphanedScope),
      );
    }
    return ScopeReconciliation(destroyedScopes: foreign);
  }
}
