import 'dart:io';

import 'package:integration_test/integration_test_driver_extended.dart';

/// Driver for the screenshot harness. `flutter drive` runs this on the host; it
/// receives each `binding.takeScreenshot(name)` from
/// integration_test/screenshots_test.dart and writes the PNG to build/screenshots/.
Future<void> main() async {
  await integrationDriver(
    onScreenshot:
        (String name, List<int> bytes, [Map<String, Object?>? args]) async {
          final dir = Directory('build/screenshots');
          if (!dir.existsSync()) dir.createSync(recursive: true);
          File('build/screenshots/$name.png').writeAsBytesSync(bytes);
          return true;
        },
  );
}
