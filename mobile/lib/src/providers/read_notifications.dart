import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../core/observability.dart';
import '../repositories/notification_repository.dart';
import 'data_providers.dart';

/// Read-only access to the legacy device-local notification state.
///
/// This store is a migration input, never an authority or UI state source.
abstract interface class LegacyNotificationReadStore {
  Future<List<String>> readIds();
  Future<void> clear();
}

class SecureLegacyNotificationReadStore implements LegacyNotificationReadStore {
  SecureLegacyNotificationReadStore([FlutterSecureStorage? storage])
      : _storage = storage ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(encryptedSharedPreferences: true),
            );

  final FlutterSecureStorage _storage;
  static const _key = 'read_notification_ids';

  @override
  Future<List<String>> readIds() async {
    final raw = await _storage.read(key: _key);
    if (raw == null) return const [];
    try {
      final decoded = jsonDecode(raw);
      if (decoded is! List) {
        await clear();
        return const [];
      }
      return decoded
          .map((value) => value.toString().trim())
          .where((value) => value.isNotEmpty)
          .toSet()
          .toList();
    } catch (_) {
      await clear();
      return const [];
    }
  }

  @override
  Future<void> clear() => _storage.delete(key: _key);
}

/// One-way handoff from the retired per-device writer to server authority.
class NotificationReadMigration {
  NotificationReadMigration({required this.repository, required this.store});

  final NotificationRepository repository;
  final LegacyNotificationReadStore store;

  /// Returns true only when legacy IDs were accepted by the server and cleared.
  Future<bool> run() async {
    final ids = await store.readIds();
    if (ids.isEmpty) return false;
    for (var start = 0; start < ids.length; start += 500) {
      await repository.markRead(ids.skip(start).take(500));
    }
    await store.clear();
    return true;
  }
}

final legacyNotificationReadStoreProvider =
    Provider<LegacyNotificationReadStore>(
  (_) => SecureLegacyNotificationReadStore(),
);

/// Migrates old local IDs once per signed-in identity. A network failure leaves
/// the IDs intact for a later retry. Rendering continues from the cached/fresh
/// server GET response, never from this legacy store.
final notificationReadMigrationProvider =
    FutureProvider.autoDispose<void>((ref) async {
  final accountId = ref.watch(accountIdProvider);
  if (accountId == null) return;
  try {
    final migrated = await NotificationReadMigration(
      repository: ref.watch(notificationRepositoryProvider),
      store: ref.watch(legacyNotificationReadStoreProvider),
    ).run();
    if (migrated) ref.invalidate(notificationsProvider);
  } catch (error) {
    Log.breadcrumb(
      'notification read-state migration deferred',
      category: 'notifications',
      data: {'error': '$error'},
    );
  }
});

class NotificationReadActions {
  NotificationReadActions({
    required this.repository,
    required this.onChanged,
  });

  final NotificationRepository repository;
  final Future<void> Function() onChanged;

  Future<void> markRead(String notificationId) async {
    await repository.markRead([notificationId]);
    await onChanged();
  }

  Future<void> markAllRead() async {
    await repository.markAllRead();
    await onChanged();
  }
}

/// Thin mobile adapter: mutations go to the server owner, then refetch the
/// canonical inbox so the dashboard and inbox consume the same state.
final notificationReadActionsProvider =
    Provider<NotificationReadActions>((ref) {
  return NotificationReadActions(
    repository: ref.watch(notificationRepositoryProvider),
    onChanged: () async {
      ref.invalidate(notificationsProvider);
      await ref.read(notificationsProvider.future);
    },
  );
});
