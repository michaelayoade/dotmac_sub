import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/core/token_storage.dart';
import 'package:dotmac_portal/src/providers/theme_controller.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  // In-memory mock for flutter_secure_storage so the real TokenStorage runs.
  const channel = MethodChannel('plugins.it_nomads.com/flutter_secure_storage');
  late Map<String, String> store;

  setUp(() {
    store = <String, String>{};
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
          final args = (call.arguments as Map?) ?? const {};
          switch (call.method) {
            case 'write':
              store[args['key'] as String] = args['value'] as String;
              return null;
            case 'read':
              return store[args['key'] as String];
            case 'delete':
              store.remove(args['key'] as String);
              return null;
            default:
              return null;
          }
        });
  });

  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, null);
  });

  test('defaults to system and persists a chosen mode', () async {
    final ts = TokenStorage();
    final controller = ThemeModeController(ts);
    expect(controller.state, ThemeMode.system);

    await controller.set(ThemeMode.dark);
    expect(controller.state, ThemeMode.dark);
    expect(await ts.readThemeMode(), 'dark');
  });

  test('loads the persisted mode on construction', () async {
    final ts = TokenStorage();
    await ts.setThemeMode('light');

    final controller = ThemeModeController(ts);
    // _load() is async — let it run.
    await Future<void>.delayed(Duration.zero);
    expect(controller.state, ThemeMode.light);
  });

  test('theme preference survives a token clear', () async {
    final ts = TokenStorage();
    await ts.setThemeMode('dark');
    await ts.clear();
    expect(await ts.readThemeMode(), 'dark');
  });
}
