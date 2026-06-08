import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/token_storage.dart';
import 'auth_controller.dart';

/// App theme preference, persisted in secure storage. Defaults to
/// [ThemeMode.system] until the stored value loads.
final themeModeProvider =
    StateNotifierProvider<ThemeModeController, ThemeMode>((ref) {
  return ThemeModeController(ref.watch(tokenStorageProvider));
});

class ThemeModeController extends StateNotifier<ThemeMode> {
  ThemeModeController(this._storage) : super(ThemeMode.system) {
    _load();
  }

  final TokenStorage _storage;
  bool _userChose = false;

  Future<void> _load() async {
    final stored = _parse(await _storage.readThemeMode());
    // Don't clobber a choice the user made while the read was in flight.
    if (!_userChose) state = stored;
  }

  Future<void> set(ThemeMode mode) async {
    _userChose = true;
    state = mode;
    await _storage.setThemeMode(mode.name);
  }

  static ThemeMode _parse(String? value) => switch (value) {
        'light' => ThemeMode.light,
        'dark' => ThemeMode.dark,
        _ => ThemeMode.system,
      };
}
