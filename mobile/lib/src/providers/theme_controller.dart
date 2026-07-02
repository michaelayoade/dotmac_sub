import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/token_storage.dart';
import 'auth_controller.dart';

/// App theme preference, persisted in secure storage. Defaults to
/// [ThemeMode.system] until the stored value loads.
final themeModeProvider = StateNotifierProvider<ThemeModeController, ThemeMode>(
  (ref) {
    return ThemeModeController(ref.watch(tokenStorageProvider));
  },
);

class ThemeModeController extends StateNotifier<ThemeMode> {
  ThemeModeController(this._storage) : super(ThemeMode.system) {
    _load();
  }

  final TokenStorage _storage;
  bool _userChose = false;

  Future<void> _load() async {
    try {
      final stored = _parse(await _storage.readThemeMode());
      // Don't clobber a choice the user made while the read was in flight.
      if (!_userChose) state = stored;
    } catch (_) {
      // A storage read failure just means we keep the default (system).
    }
  }

  Future<void> set(ThemeMode mode) async {
    _userChose = true;
    state = mode; // optimistic — the UI updates immediately
    try {
      await _storage.setThemeMode(mode.name);
    } catch (_) {
      // Persisting failed; the theme still applies for this session.
    }
  }

  static ThemeMode _parse(String? value) => switch (value) {
    'light' => ThemeMode.light,
    'dark' => ThemeMode.dark,
    _ => ThemeMode.system,
  };
}
