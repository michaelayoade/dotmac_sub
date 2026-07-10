import 'package:flutter/material.dart';

/// Industrial Modern, outdoors: high contrast, calm status colors,
/// glove-grade touch targets. Mirrors the web design system tokens.
abstract final class AppColors {
  static const primary = Color(0xFF06B6D4); // teal
  static const accent = Color(0xFFF97316); // warm orange

  // Work-type colors (left card bars, chips) — same mapping as dispatch web.
  static const workTypeColors = <String, Color>{
    'install': Color(0xFFF59E0B), // amber
    'repair': Color(0xFFF43F5E), // rose
    'survey': Color(0xFF8B5CF6), // violet
    'maintenance': Color(0xFF06B6D4), // cyan
    'disconnect': Color(0xFF64748B), // slate
    'other': Color(0xFF64748B),
  };

  // One status ramp shared with dispatch.
  static const statusColors = <String, Color>{
    'scheduled': Color(0xFF64748B), // slate
    'dispatched': Color(0xFF6366F1), // indigo
    'accepted': Color(0xFF06B6D4), // cyan
    'en_route': Color(0xFFF97316), // orange
    'in_progress': Color(0xFF14B8A6), // teal
    'paused': Color(0xFFF59E0B), // amber
    'completed': Color(0xFF10B981), // emerald
    'hold': Color(0xFFF59E0B), // amber
    'canceled': Color(0xFF94A3B8),
  };

  static Color workType(String type) =>
      workTypeColors[type] ?? workTypeColors['other']!;
  static Color status(String status) =>
      statusColors[status] ?? statusColors['scheduled']!;
}

abstract final class AppRadii {
  static const card = 16.0;
  static const control = 12.0;
  static const chip = 8.0;
}

abstract final class AppSizes {
  /// Glove-grade primary actions.
  static const primaryTouchTarget = 56.0;
  static const touchTarget = 48.0;
}

ThemeData _base(Brightness brightness) {
  final scheme = ColorScheme.fromSeed(
    seedColor: AppColors.primary,
    brightness: brightness,
    primary: AppColors.primary,
    secondary: AppColors.accent,
  );
  final isDark = brightness == Brightness.dark;
  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: isDark
        ? const Color(0xFF0F172A)
        : const Color(0xFFF1F5F9),
    cardTheme: CardThemeData(
      elevation: isDark ? 0 : 1,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(AppRadii.card),
      ),
      margin: EdgeInsets.zero,
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        minimumSize: const Size.fromHeight(AppSizes.primaryTouchTarget),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(AppRadii.control),
        ),
        textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        minimumSize: const Size.fromHeight(AppSizes.touchTarget),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(AppRadii.control),
        ),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(AppRadii.control),
      ),
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 16),
    ),
    chipTheme: ChipThemeData(
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(AppRadii.chip),
      ),
    ),
    navigationBarTheme: NavigationBarThemeData(
      height: 72,
      labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
    ),
  );
}

final lightTheme = _base(Brightness.light);
final darkTheme = _base(Brightness.dark);
