import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Per-device read state for the notifications inbox.
///
/// The backend Notification model has no `read_at` column, so "read" is tracked
/// locally (persisted) until a synced backend field exists. The set is pruned
/// to currently-known ids to keep it bounded.
class ReadNotifications extends StateNotifier<Set<String>> {
  ReadNotifications([FlutterSecureStorage? storage])
      : _storage = storage ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(encryptedSharedPreferences: true),
            ),
        super(const {}) {
    _load();
  }

  final FlutterSecureStorage _storage;
  static const _key = 'read_notification_ids';

  Future<void> _load() async {
    final raw = await _storage.read(key: _key);
    if (raw == null) return;
    try {
      final ids = (jsonDecode(raw) as List).map((e) => e.toString()).toSet();
      state = ids;
    } catch (_) {
      // ignore corrupt state
    }
  }

  Future<void> _persist() async {
    await _storage.write(key: _key, value: jsonEncode(state.toList()));
  }

  bool isRead(String id) => state.contains(id);

  Future<void> markRead(String id) async {
    if (state.contains(id)) return;
    state = {...state, id};
    await _persist();
  }

  Future<void> markAllRead(Iterable<String> ids) async {
    final next = {...state, ...ids};
    if (next.length == state.length) return;
    state = next;
    await _persist();
  }

  /// Drop ids no longer present in the inbox so the set stays bounded.
  Future<void> prune(Iterable<String> knownIds) async {
    final known = knownIds.toSet();
    final next = state.intersection(known);
    if (next.length == state.length) return;
    state = next;
    await _persist();
  }
}

final readNotificationsProvider =
    StateNotifierProvider<ReadNotifications, Set<String>>(
  (ref) => ReadNotifications(),
);
