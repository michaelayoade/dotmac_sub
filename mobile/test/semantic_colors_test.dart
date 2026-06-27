import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/app.dart';
import 'package:dotmac_portal/src/core/semantic_colors.dart';

void main() {
  group('SemanticColors extension', () {
    test('light and dark variants differ (so dark mode adapts)', () {
      expect(SemanticColors.light.success, isNot(SemanticColors.dark.success));
      expect(SemanticColors.light.warning, isNot(SemanticColors.dark.warning));
    });

    test('lerp interpolates each channel', () {
      final mid = SemanticColors.light.lerp(SemanticColors.dark, 0.5);
      expect(
        mid.success,
        Color.lerp(
            SemanticColors.light.success, SemanticColors.dark.success, 0.5),
      );
    });

    test('copyWith overrides only the named field', () {
      final c = SemanticColors.light.copyWith(success: const Color(0xFF123456));
      expect(c.success, const Color(0xFF123456));
      expect(c.warning, SemanticColors.light.warning);
    });

    testWidgets('context.semantic resolves the registered extension per theme',
        (tester) async {
      late SemanticColors seen;
      Future<void> pump(ThemeMode mode) => tester.pumpWidget(MaterialApp(
            theme: dotmacThemeFor(Brightness.light),
            darkTheme: dotmacThemeFor(Brightness.dark),
            themeMode: mode,
            home: Builder(builder: (context) {
              seen = context.semantic;
              return const SizedBox();
            }),
          ));

      await pump(ThemeMode.light);
      await tester.pumpAndSettle();
      expect(seen.success, SemanticColors.light.success);

      await pump(ThemeMode.dark);
      await tester.pumpAndSettle(); // let AnimatedTheme cross-fade complete
      expect(seen.success, SemanticColors.dark.success);
    });
  });
}
